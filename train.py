#!/usr/bin/env python3

import tqdm
import argparse
import os
import numpy as np

import torch
import torch.optim as optim
import torchvision.utils as vutils
from torch.autograd import Variable

import my_utils
from structured_container import DataContainer
from models import *
import models

PAE_BATCH_SIZE = 4
GAN_BATCH_SIZE = 16
AVERAGING_BATCH_SIZE = 16
EP_LEN = 100

AVERAGING_ERROR_MULTIPLIER = 500
AVERAGING_FUTURE_ERROR_MULTIPLIER = 500
N_STEPS_AHEAD = 4

BALLS_OBS_SHAPE = (1, 28, 28)

GUARANTEED_PERCEPTS = 4
UNCERTAIN_PERCEPTS = 4
P_NO_OBS_VALID = 1.0

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Train network based on time-series data.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--output_dir', default='default_experiment', type=str,
                        help="Folder for the outputs.")
    parser.add_argument('--dataset_type', default='balls', type=str,
                        choices=['balls', 'atari', 'stocks'],
                        help="Type of the dataset.")
    parser.add_argument('--data_dir', type=str,
                        help="Folder with the data")
    parser.add_argument('--start_from_checkpoint', type=int,
                        help="Use network that was trained already")
    parser.add_argument('--epochs', default=10, type=int,
                        help="How many epochs to train for?")
    parser.add_argument('--updates_per_epoch', default=3000, type=int,
                        help="How many updates per epoch?")
    parser.add_argument('--training_stage', type=str,
                        choices=['pae', 'paegan-sampler', 'visual-sampler', 'averager', 'future-sampler'],
                        help="Different training modes enable training of different parts of the network")
    parser.add_argument('--p_mask', default=0.99, type=float,
                        help="What fraction of input observations is masked? eg 0.6")
    parser.add_argument('--av_loss', default=AVERAGING_FUTURE_ERROR_MULTIPLIER, type=float,
                        help="Multiplier for the future averaging loss. eg 500")
    parser.add_argument('--reward_only_masked', default=0, type=int,
                        choices=[0, 1],
                        help="Should pae train only from masked observations error or all? Use only if observations are"
                             "noisy.")
    parser.add_argument('--compare_with_pf', default=0, type=int,
                        choices=[0, 1],
                        help="Should the results be compared with particle filter?")
    parser.add_argument('--cuda', default=1, type=int,
                        choices=[0, 1],
                        help="Should CUDA be used?")

    parser.print_help()
    args = parser.parse_args()
    print(args)

    output_dir = args.output_dir
    n_epochs = args.epochs
    updates_per_epoch = args.updates_per_epoch
    training_stage = args.training_stage
    p_mask = args.p_mask
    av_loss_multiplier = args.av_loss
    use_cuda = bool(args.cuda)
    compare_with_pf = bool(args.compare_with_pf)
    reward_only_masked = bool(args.reward_only_masked)
    train_d_every_n_updates = 1

    train_pae_switch = False
    train_d_switch = False
    train_g_switch = False
    train_av_switch = False
    train_av_future_switch = False

    if training_stage == "pae":
        train_pae_switch = True
    elif training_stage == "visual-sampler":
        train_d_every_n_updates = 5
        train_d_switch = True
        train_g_switch = True
        train_av_switch = True
    elif training_stage == "future-sampler":
        train_d_every_n_updates = 7
        train_d_switch = True
        train_g_switch = True
        train_av_future_switch = True
    else:
        raise ValueError('Wrong training stage {}'.format(training_stage))

    if use_cuda:
        assert torch.cuda.is_available() is True

    if not os.path.exists(args.output_dir):
        my_utils.make_dir_tree(args.output_dir)

    # prepare data
    sim_config = None
    obs_shape = None
    train_getter = None
    valid_getter = None
    if args.dataset_type == 'balls':
        sim_config = torch.load('{}/train.conf'.format(args.data_dir))
        obs_shape = BALLS_OBS_SHAPE

        train_container = DataContainer('{}/train.pt'.format(args.data_dir), batch_size=PAE_BATCH_SIZE)
        valid_container = DataContainer('{}/valid.pt'.format(args.data_dir), batch_size=PAE_BATCH_SIZE)
        sim_config = torch.load(open('{}/train.conf'.format(args.data_dir), 'rb'))

        train_container.populate_images()
        valid_container.populate_images()

        train_getter = train_container.get_batch_episodes
        valid_getter = valid_container.get_batch_episodes

    else:
        raise ValueError('Failed to load data. Wrong dataset type {}'.format(args.dataset_type))

    if compare_with_pf:
        assert sim_config is not None

    # prepare network
    if args.dataset_type == 'balls':
        net = PAEGAN()
        noise_size = models.N_SIZE
        bs_size = models.BS_SIZE

    else:
        raise ValueError('Failed to initialise model. Wrong dataset type {}'.format(args.dataset_type))

    if args.start_from_checkpoint is not None:
        assert type(args.start_from_checkpoint) == int
        net.load_state_dict(torch.load("{}/network/paegan_epoch_{}.pth".format(args.output_dir, args.start_from_checkpoint)))
        current_epoch = args.start_from_checkpoint + 1
    else:
        current_epoch = 0

    # initialise variables
    real_label = 1
    fake_label = 0
    if use_cuda:
        print("Using CUDA.")
        net = net.cuda()
        criterion_pae = nn.MSELoss().cuda()
        criterion_gan = nn.BCELoss().cuda()
        # criterion_gan = nn.MSELoss().cuda()
        criterion_gen_averaged = nn.MSELoss().cuda()

        obs_in = Variable(torch.FloatTensor(EP_LEN, PAE_BATCH_SIZE, *BALLS_OBS_SHAPE).cuda())
        obs_out = Variable(torch.FloatTensor(EP_LEN, PAE_BATCH_SIZE, *BALLS_OBS_SHAPE).cuda())

        averaging_noise = Variable(torch.FloatTensor(AVERAGING_BATCH_SIZE, noise_size).cuda())
        g_noise = Variable(torch.FloatTensor(GAN_BATCH_SIZE, noise_size).cuda())

        fixed_noise = Variable(torch.FloatTensor(GAN_BATCH_SIZE, noise_size).normal_(0, 1).cuda())
        fixed_bs_noise = Variable(torch.FloatTensor(GAN_BATCH_SIZE, bs_size).uniform_(-1, 1).cuda())
        fake_labels = Variable(torch.FloatTensor(GAN_BATCH_SIZE, 1).cuda())
        real_labels = Variable(torch.FloatTensor(GAN_BATCH_SIZE, 1).cuda())
        null_observation = Variable(torch.FloatTensor(1, *BALLS_OBS_SHAPE).cuda())
    else:
        print("Not using CUDA.")
        net.cpu()
        criterion_pae = nn.MSELoss()
        criterion_gan = nn.BCELoss()
        # criterion_gan = nn.MSELoss()
        criterion_gen_averaged = nn.MSELoss()

        obs_in = Variable(torch.FloatTensor(EP_LEN, PAE_BATCH_SIZE, *BALLS_OBS_SHAPE))
        obs_out = Variable(torch.FloatTensor(EP_LEN, PAE_BATCH_SIZE, *BALLS_OBS_SHAPE))

        averaging_noise = Variable(torch.FloatTensor(AVERAGING_BATCH_SIZE, noise_size))
        g_noise = Variable(torch.FloatTensor(GAN_BATCH_SIZE, noise_size))

        fixed_noise = Variable(torch.FloatTensor(GAN_BATCH_SIZE, noise_size).normal_(0, 1))
        fixed_bs_noise = Variable(torch.FloatTensor(GAN_BATCH_SIZE, bs_size).uniform_(-1, 1))
        fake_labels = Variable(torch.FloatTensor(GAN_BATCH_SIZE, 1))
        real_labels = Variable(torch.FloatTensor(GAN_BATCH_SIZE, 1))
        null_observation = Variable(torch.FloatTensor(1, *BALLS_OBS_SHAPE))

    null_observation.data.fill_(0)

    # optimisers
    optimiser_pae = optim.Adam([{'params': net.bs_prop.parameters()},
                                {'params': net.decoder.parameters()}],
                               lr=0.0003)
    optimiser_g = optim.Adam(net.G.parameters(), lr=0.0002)
    optimiser_d = optim.Adam(net.D.parameters(), lr=0.0002)

    # start training
    epoch_report = {}
    until_epoch = current_epoch + n_epochs + 1
    for current_epoch in range(current_epoch, until_epoch):

        bar = tqdm.trange(updates_per_epoch)
        epoch_report['epoch'] = '[{}/{}]'.format(current_epoch, until_epoch)

        for update in bar:
            net.zero_grad()
            losses = []

            batch = train_getter()
            masked, masked_indices = my_utils.mask_percepts(batch, p=p_mask, return_indices=True)

            batch = batch.transpose((1, 0, 4, 2, 3))
            masked = masked.transpose((1, 0, 4, 2, 3))

            batch = torch.FloatTensor(batch)
            masked = torch.FloatTensor(masked)

            obs_in.data.copy_(masked)
            obs_out.data.copy_(batch)

            # generate beliefs states
            # _ep means tensor has shape (ep_len, batch_size, *obs_shape)
            # _nonep means tensor has shape (ep_len * batch_size, *obs_shape)
            states_ep = net.bs_prop(obs_in)
            states_nonep = states_ep.view(EP_LEN * PAE_BATCH_SIZE, -1)

            obs_expectation = None
            if train_av_switch and not train_pae_switch:
                obs_expectation = net.decoder(states_nonep).view(obs_in.size())

            elif train_pae_switch is True:
                obs_expectation = net.decoder(states_nonep).view(obs_in.size())
                if reward_only_masked:
                    masked_indices = torch.ByteTensor(masked_indices.astype('int')).nonzero()
                    if use_cuda:
                        masked_indices = masked_indices.cuda()
                    err_pae = criterion_pae(obs_expectation[masked_indices, :, ...], obs_out[masked_indices, :, ...])
                    # err_pae_full = 0.05 * criterion_pae(obs_expectation, obs_out)
                    # losses.append(err_pae_full)
                else:
                    err_pae = criterion_pae(obs_expectation, obs_out)
                losses.append(err_pae)
                epoch_report['pae train loss'] = err_pae.data[0]

            if train_d_switch is True and update % train_d_every_n_updates == 0:
                real_labels.data.fill_(real_label)
                fake_labels.data.fill_(fake_label)

                obs_out_nonep = obs_out.view(EP_LEN * PAE_BATCH_SIZE,
                                             obs_out.size(2), obs_out.size(3), obs_out.size(4))
                # draw real observations for D training
                draw = np.random.choice(EP_LEN * PAE_BATCH_SIZE, size=GAN_BATCH_SIZE, replace=False)
                obs_d = obs_out_nonep[draw, ...]

                # draw states for D training
                draw = np.random.choice(EP_LEN * PAE_BATCH_SIZE, size=GAN_BATCH_SIZE, replace=False)
                states_d = states_nonep[draw, ...]

                # train discriminator with real data
                out_d_real = net.D(obs_d).view(GAN_BATCH_SIZE, 1)
                # print("out_d_real", out_d_real)
                err_d_real = criterion_gan(out_d_real, real_labels)

                # train discriminator with fake data
                g_noise.data.normal_(0, 1)
                state_sample = net.G(g_noise, states_d)
                obs_sample = net.decoder(state_sample)
                out_d_fake = net.D(obs_sample.detach()).view(GAN_BATCH_SIZE, 1)
                # print("out_d_fake", out_d_fake)
                err_d_fake = criterion_gan(out_d_fake, fake_labels)

                err_d = (err_d_fake + err_d_real) / 2
                # losses.append(err_d)

                err_d.backward()
                optimiser_d.step()

                epoch_report['d loss'] = err_d.data[0]

                if update == 0:
                    vutils.save_image(obs_d.data,
                                      '{}/images/real_samples.png'.format(output_dir),
                                      normalize=True)

            if train_g_switch is True:
                # train generator using discriminator
                # draw states for G training
                draw = np.random.choice(EP_LEN * PAE_BATCH_SIZE, size=GAN_BATCH_SIZE, replace=False)
                states_g = states_nonep[draw, ...]

                g_noise.data.normal_(0, 1)
                state_sample = net.G(g_noise, states_g.detach())
                obs_sample = net.decoder(state_sample)

                out_d_g = net.D(obs_sample).view(GAN_BATCH_SIZE, 1)
                # print("out_d_g", out_d_g)
                err_g = criterion_gan(out_d_g, real_labels)
                losses.append(err_g)

                epoch_report['g loss'] = err_g.data[0]

                if update % 100 == 0:
                    state_sample = net.G(fixed_noise, states_g)
                    obs_sample = net.decoder(state_sample)
                    vutils.save_image(obs_sample.data,
                                      '{}/images/fake_samples_epoch_{}.png'.format(output_dir, current_epoch),
                                      normalize=False)

            if train_av_switch is True:
                # train generator using averaging
                # draw random states
                draw = np.random.choice(EP_LEN * PAE_BATCH_SIZE, size=1, replace=False)
                states_av = states_nonep[draw, ...]
                states_av_expanded = states_av.expand(AVERAGING_BATCH_SIZE, -1)

                # get corresponding observation expectation
                obs_exp_nonep = obs_expectation.view(EP_LEN * PAE_BATCH_SIZE,
                                                     obs_out.size(2), obs_out.size(3), obs_out.size(4))
                obs_exp = obs_exp_nonep[draw, ...]

                # generate samples from state
                averaging_noise.data.normal_(0, 1)
                n_samples = net.G(averaging_noise, states_av_expanded.detach())

                n_recons = net.decoder(n_samples)
                sample_av = n_recons.mean(dim=0).unsqueeze(0)

                err_av = criterion_gen_averaged(sample_av, obs_exp.detach())

                # normalise error to ~1
                losses.append(av_loss_multiplier * err_av)
                epoch_report['av loss'] = err_av.data[0]

                if update % 50 == 0:
                    sample_mixture = sample_av.data.cpu().numpy()
                    observation_belief = obs_exp.data.cpu().numpy()
                    joint = np.concatenate((observation_belief, sample_mixture), axis=-2)
                    joint = np.expand_dims(joint, axis=0)
                    my_utils.batch_to_sequence(joint, fpath='{}/images/sample_av_{}.gif'.format(output_dir, current_epoch))

            if train_av_future_switch is True:
                assert train_pae_switch is False

                n_steps_ahead = int(np.random.randint(1, N_STEPS_AHEAD))

                # draw random states
                draw = np.random.choice(EP_LEN * PAE_BATCH_SIZE, size=1, replace=False)
                states_av_fut = states_nonep[draw, ...]
                states_av_fut_expanded = states_av_fut.expand(AVERAGING_BATCH_SIZE, -1)

                # generate samples from state
                averaging_noise.data.normal_(0, 1)
                n_samples_fut = net.G(averaging_noise, states_av_fut_expanded.detach())

                # get null update (from null observation)
                null_update = net.bs_prop.encoder(null_observation).detach()
                # print(null_update.size())
                null_update_expanded1 = null_update.expand(n_steps_ahead, 1, -1)
                # print(null_update_expanded1.size())
                null_update_expanded2 = null_update.expand(n_steps_ahead, AVERAGING_BATCH_SIZE, -1)
                # print(null_update_expanded2.size())

                # propagate belief in time
                _, future_belief = net.bs_prop.gru(null_update_expanded1, hx=states_av_fut.view(1, 1, -1))
                # print(future_belief.size())
                future_exp = net.decoder(future_belief.view(1, -1))

                # propagate samples in time
                _, future_samples = net.bs_prop.gru(null_update_expanded2, hx=n_samples_fut.view(1, AVERAGING_BATCH_SIZE, -1))
                # print(future_samples.size())
                future_recons = net.decoder(future_samples.view(AVERAGING_BATCH_SIZE, -1))

                future_av = future_recons.mean(dim=0).unsqueeze(0)

                err_future_av = criterion_gen_averaged(future_av, future_exp.detach())

                # normalise error to ~1

                losses.append(av_loss_multiplier * err_future_av)
                epoch_report['av fut loss'] = err_future_av.data[0]

                if update % 50 == 0:
                    sample_mixture = future_av.data.cpu().numpy()
                    observation_belief = future_exp.data.cpu().numpy()
                    joint = np.concatenate((observation_belief, sample_mixture), axis=-2)
                    joint = np.expand_dims(joint, axis=0)
                    my_utils.batch_to_sequence(joint,
                                                  fpath='{}/images/future_av_{}.gif'.format(output_dir, current_epoch))

            # =====================================
            # UPDATE WEIGHTS HERE!
            if len(losses) > 0:
                sum(losses).backward()

            if train_pae_switch:
                optimiser_pae.step()

            if train_g_switch or train_av_switch:
                optimiser_g.step()

            # pae validation error and image record
            if update % 100 == 0:
                batch = valid_getter()
                masked, masked_indices = my_utils.mask_percepts(batch, p=p_mask, return_indices=True)

                batch = batch.transpose((1, 0, 4, 2, 3))
                masked = masked.transpose((1, 0, 4, 2, 3))

                batch = torch.FloatTensor(batch)
                masked = torch.FloatTensor(masked)

                obs_in.data.copy_(masked)
                obs_out.data.copy_(batch)

                # generate beliefs states
                states_ep = net.bs_prop(obs_in)
                states_nonep = states_ep.view(EP_LEN * PAE_BATCH_SIZE, -1)

                obs_expectation = net.decoder(states_nonep).view(obs_in.size())

                if reward_only_masked:
                    masked_indices = torch.ByteTensor(masked_indices.astype('int')).nonzero()
                    if use_cuda:
                        masked_indices = masked_indices.cuda()
                    err_valid_pae = criterion_pae(obs_expectation[masked_indices, :, ...], obs_out[masked_indices, :, ...])
                else:
                    err_valid_pae = criterion_pae(obs_expectation, obs_out)
                epoch_report['pae valid loss'] = err_valid_pae.data[0]

                # print a gif
                if update % 500 == 0:
                    recon_ims = obs_expectation.data.cpu().numpy()
                    target_ims = obs_out.data.cpu().numpy()
                    joint = np.concatenate((target_ims, recon_ims), axis=-2)
                    my_utils.batch_to_sequence(joint, fpath='{}/images/valid_recon_{}.gif'.format(output_dir, current_epoch))

            bar.set_postfix(**epoch_report)

        torch.save(net.state_dict(), '{}/network/paegan_epoch_{}.pth'.format(output_dir, current_epoch))
        if compare_with_pf:
            my_utils.pf_comparison(net, sim_config, output_dir, current_epoch)


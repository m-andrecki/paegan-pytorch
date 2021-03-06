import os
import numpy as np
import imageio
import pandas as pd

from train import EP_LEN, UNCERTAIN_PERCEPTS, GUARANTEED_PERCEPTS

FOLDERS = ['images', 'network', 'numerical', 'plots', 'page']


def make_dir_tree(parent_dir):
    for folder in FOLDERS:
        new_dir = '{}/{}'.format(parent_dir, folder)
        if not os.path.exists(new_dir):
            os.makedirs(new_dir)


def mask_percepts(images, p, return_indices=False):
    images_masked = np.copy(images)
    if p < 1.0:
        for_removal = np.random.random(EP_LEN) < p
    else:
        for_removal = np.ones(EP_LEN) > 0

    if UNCERTAIN_PERCEPTS > 0:
        clear_percepts = GUARANTEED_PERCEPTS + np.random.randint(0, UNCERTAIN_PERCEPTS)
    else:
        clear_percepts = GUARANTEED_PERCEPTS
    for_removal[0:clear_percepts] = False
    images_masked[:, for_removal, ...] = 0

    if return_indices:
        return images_masked, for_removal
    else:
        return images_masked


def batch_to_sequence(batch_eps, fpath, normalise=False):
    """

    :param batch_eps: in format (timesteps, batchs_size, im_channels, im_height, im_width)
    :param fpath:
    :return:
    """
    batch_eps = [batch_eps[:, i, ...] for i in range(batch_eps.shape[1])]
    batch_eps = np.concatenate(batch_eps, axis=-1)

    im_seq = []
    for i in range(batch_eps.shape[0]):
        im_seq.append(batch_eps[i, 0, :, :])

    imageio.mimsave(fpath, im_seq)

from particle_filter import ParticleFilter
from balls_sim import World
from torch.autograd import Variable
import torch
import matplotlib.pyplot as plt


def pf_multi_run_plot(net, sim_conf, fpath='ims/last_test.csv', cuda=True, runs=10, p_mask=1.0, n_particles=100, gif_no=0):
    CONSISTENT_NOISE = False
    RUN_LENGTH = 160
    DURATION = 0.4
    N_SIZE = 256

    pf_loss_ar = np.zeros(RUN_LENGTH)
    pae_loss_ar = np.zeros(RUN_LENGTH)

    for _ in range(runs):
        w = World(**sim_conf)

        pf = ParticleFilter(sim_conf, n_particles=n_particles)

        pos = [body.pos for body in w.bodies]
        vel = [body.vel for body in w.bodies]
        pf.warm_start(pos, vel=vel)

        ims_percept = []
        ims_pf_belief = []
        ims_pf_sample = []

        loss_pf = []
        loss_sample_mse = []

        masked_percepts = np.zeros(RUN_LENGTH) < 1
        for i in range(RUN_LENGTH):
            if i < 8 or np.random.rand() > p_mask:
                measures = [body.pos + sim_conf['measurement_noise'] * np.random.randn(2) for body in w.bodies]
                pf.update(measures)
                masked_percepts[i] = False
                pf.resample()
            else:
                masked_percepts[i] = True

            w.run()
            pf.predict()

            percept = w.draw()
            belief = pf.draw()[:, :, 0]
            sample = pf.parts[np.random.randint(pf.n)].draw()

            loss_pf.append(np.mean((percept - belief) ** 2))
            loss_sample_mse.append(np.mean((percept - sample) ** 2))

            ims_percept.append(percept)
            ims_pf_belief.append(belief)
            ims_pf_sample.append(sample)

        # run predictions with the network
        x = np.array(ims_percept)
        x = x.reshape((1, RUN_LENGTH, 28, 28, 1))
        x[:, masked_percepts, ...] = 0

        x = x.transpose((1, 0, 4, 2, 3))
        x = Variable(torch.FloatTensor(x))

        if cuda:
            net = net.cuda()
            x = x.cuda()

        states = net.bs_prop(x)

        # create expected observations
        obs_expectation = net.decoder(states)
        obs_expectation = obs_expectation.view(x.size())

        obs_expectation = obs_expectation.data.cpu().numpy()
        obs_expectation = obs_expectation.reshape((RUN_LENGTH, 28, 28))

        # create observation samples (constant or varying noise accross time)
        if CONSISTENT_NOISE is True:
            noise = Variable(torch.FloatTensor(1, N_SIZE))
            noise.data.normal_(0, 1)
            noise = noise.expand(RUN_LENGTH, N_SIZE)
        else:
            noise = Variable(torch.FloatTensor(RUN_LENGTH, N_SIZE))
            noise.data.normal_(0, 1)

        if cuda:
            noise = noise.cuda()

        # states_non_ep = states.unfold(0, 1, (EP_LEN*BATCH_SIZE)//GAN_BATCH_SIZE).squeeze(-1)

        pae_samples = net.G(noise, states.squeeze_(1))
        pae_samples = net.decoder(pae_samples)
        pae_samples = pae_samples.view(x.size())

        pae_samples = pae_samples.data.cpu().numpy()
        pae_samples = pae_samples.reshape((RUN_LENGTH, 28, 28))

        pae_ims = []
        pae_samples_ims = []
        loss_pae = []
        for i in range(RUN_LENGTH):
            pae_ims.append(obs_expectation[i, ...])
            pae_samples_ims.append(pae_samples[i, ...])
            loss_pae.append(np.mean((ims_percept[i] - obs_expectation[i, ...]) ** 2))

        pf_loss_ar += np.array(loss_pf)
        pae_loss_ar += np.array(loss_pae)

    ims_ar = np.array(ims_percept)
    av_pixel_intensity = np.mean(ims_ar)
    baseline_level = np.mean((ims_ar - av_pixel_intensity) ** 2)
    baseline = np.ones(len(loss_pf)) * baseline_level
    # print("Uninformative baseline level at {}".format(baseline_level))

    pf_loss_ar /= runs
    pae_loss_ar /= runs
    baseline_ar = baseline

    df = pd.DataFrame({'pf loss': pf_loss_ar,
                  'pae loss': pae_loss_ar,
                  'baseline': baseline_ar
    })

    df.to_csv(fpath)

    plt.plot(pf_loss_ar)
    plt.plot(pae_loss_ar)
    plt.plot(baseline, 'g--')

    plt.title("Image reconstruction loss vs timestep")
    plt.ylabel("loss (MSE)")
    plt.xlabel("timestep")
    plt.legend(["PF", "PAE", "baseline"], loc=4)

    plt.savefig("ims/{}-plot.png".format(gif_no))
    # plt.show()
    plt.clf()

    imageio.mimsave("ims/{}-percept.gif".format(gif_no), ims_percept, duration=DURATION)
    imageio.mimsave("ims/{}-pf_belief.gif".format(gif_no), ims_pf_belief, duration=DURATION)
    imageio.mimsave("ims/{}-pf_sample.gif".format(gif_no), ims_pf_sample, duration=DURATION)
    imageio.mimsave("ims/{}-pae_belief.gif".format(gif_no), pae_ims, duration=DURATION)
    imageio.mimsave("ims/{}-pae_sample.gif".format(gif_no), pae_samples_ims, duration=DURATION)

    page = """
    <html>
    <body>
    Configuration: {1}
    <br>
    <img src="{0}-plot.png" align="center">
    <table>
      <tr>
        <th>Ground truth</th>
        <th>Particle Filter</th>
        <th>PF Sample</th>
        <th>Predictive AE</th>
        <th>PAE Sample</th>
      </tr>
      <tr>
        <td><img src="{0}-percept.gif" width="140"></td>
        <td><img src="{0}-pf_belief.gif" width="140"></td>
        <td><img src="{0}-pf_sample.gif" width="140"></td>
        <td><img src="{0}-pae_belief.gif" width="140"></td>
        <td><img src="{0}-pae_sample.gif" width="140"></td>

      </tr>

    </table>
    </body>
    </html>
    """.format(gif_no, sim_conf)

    with open("ims/page-{}.html".format(gif_no), 'w') as f:
        f.write(page)


def pf_comparison(net, sim_conf, path, gif_no, cuda=True):
    CONSISTENT_NOISE = False
    RUN_LENGTH = 160
    N_PARTICLES = 400
    DURATION = 0.3
    N_SIZE = 256

    w = World(**sim_conf)

    pf = ParticleFilter(sim_conf, n_particles=N_PARTICLES)

    pos = [body.pos for body in w.bodies]
    vel = [body.vel for body in w.bodies]
    pf.warm_start(pos, vel=vel)

    ims = []

    ims_percept = []
    ims_pf_belief = []
    ims_pf_sample = []

    loss_mse = []
    loss_sample_mse = []
    loss_mae = []

    for i in range(RUN_LENGTH):
        if i < 8:
            measures = [body.pos + sim_conf['measurement_noise'] * np.random.randn(2)for body in w.bodies]
            pf.update(measures)
            pf.resample()

        w.run()
        pf.predict()

        percept = w.draw()
        belief = pf.draw()[:, :, 0]
        sample = pf.parts[np.random.randint(pf.n)].draw()

        loss_mse.append(np.mean((percept - belief) ** 2))
        loss_sample_mse.append(np.mean((percept - sample) ** 2))

        ims_percept.append(percept)
        ims_pf_belief.append(belief)
        ims_pf_sample.append(sample)

    # run predictions with the network
    x = np.array(ims_percept)
    x = x.reshape((1, RUN_LENGTH, 28, 28, 1))
    x[:, 8:, ...] = 0

    x = x.transpose((1, 0, 4, 2, 3))
    x = Variable(torch.FloatTensor(x))

    if cuda:
        x = x.cuda()

    states = net.bs_prop(x)

    # create expected observations
    obs_expectation = net.decoder(states)
    obs_expectation = obs_expectation.view(x.size())

    obs_expectation = obs_expectation.data.cpu().numpy()
    obs_expectation = obs_expectation.reshape((RUN_LENGTH, 28, 28))

    # create observation samples (constant or varying noise accross time)
    if CONSISTENT_NOISE is True:
        noise = Variable(torch.FloatTensor(1, N_SIZE))
        noise.data.normal_(0, 1)
        noise = noise.expand(RUN_LENGTH, N_SIZE)
    else:
        noise = Variable(torch.FloatTensor(RUN_LENGTH, N_SIZE))
        noise.data.normal_(0, 1)

    if cuda:
        noise = noise.cuda()

    # states_non_ep = states.unfold(0, 1, (EP_LEN*BATCH_SIZE)//GAN_BATCH_SIZE).squeeze(-1)

    pae_samples = net.G(noise, states.squeeze_(1))
    pae_samples = net.decoder(pae_samples)
    pae_samples = pae_samples.view(x.size())

    pae_samples = pae_samples.data.cpu().numpy()
    pae_samples = pae_samples.reshape((RUN_LENGTH, 28, 28))

    pae_ims = []
    pae_samples_ims = []
    loss_pae = []
    for i in range(RUN_LENGTH):
        pae_ims.append(obs_expectation[i, ...])
        pae_samples_ims.append(pae_samples[i, ...])
        loss_pae.append(np.mean((ims_percept[i] - obs_expectation[i, ...]) ** 2))

    imageio.mimsave("{}/page/{}-percept.gif".format(path, gif_no), ims_percept, duration=DURATION)
    imageio.mimsave("{}/page/{}-pf_belief.gif".format(path, gif_no), ims_pf_belief, duration=DURATION)
    imageio.mimsave("{}/page/{}-pf_sample.gif".format(path, gif_no), ims_pf_sample, duration=DURATION)
    imageio.mimsave("{}/page/{}-pae_belief.gif".format(path, gif_no), pae_ims, duration=DURATION)
    imageio.mimsave("{}/page/{}-pae_sample.gif".format(path, gif_no), pae_samples_ims, duration=DURATION)

    page = """
    <html>
    <body>
    Configuration: {1}
    <img src="{0}-plot.png" align="center">
    <table>
      <tr>
        <th>Ground truth</th>
        <th>Particle Filter</th>
        <th>PF Sample</th>
        <th>Predictive AE</th>
        <th>PAE Sample</th>
      </tr>
      <tr>
        <td><img src="{0}-percept.gif" width="140"></td>
        <td><img src="{0}-pf_belief.gif" width="140"></td>
        <td><img src="{0}-pf_sample.gif" width="140"></td>
        <td><img src="{0}-pae_belief.gif" width="140"></td>
        <td><img src="{0}-pae_sample.gif" width="140"></td>

      </tr>

    </table>
    </body>
    </html>
    """.format(gif_no, sim_conf)

    with open("{}/page/page-{}.html".format(path, gif_no), 'w') as f:
        f.write(page)

    ims_ar = np.array(ims_percept)
    av_pixel_intensity = np.mean(ims_ar)
    baseline_level = np.mean((ims_ar - av_pixel_intensity) ** 2)
    baseline = np.ones(len(loss_mse)) * baseline_level
    print("Uninformative baseline level at {}".format(baseline_level))

    plt.plot(loss_mse)
    plt.plot(loss_pae)
    plt.plot(baseline, 'g--')

    plt.title("Image reconstruction loss vs timestep")
    plt.ylabel("loss (MSE)")
    plt.xlabel("timestep")
    plt.legend(["PF", "PAE", "baseline"])

    plt.savefig("{}/page/{}-plot.png".format(path, gif_no))
    plt.close()

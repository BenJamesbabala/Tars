import numpy as np
import theano
import theano.tensor as T
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from progressbar import ProgressBar

from ..utils import gauss_gauss_kl


class VRNN(object):
    # TODO:iwae lowerbound

    def __init__(self, prior, q, p, f, n_batch, optimizer,
                 l=1, k=1, alpha=None, random=1234):
        self.prior = prior
        self.q = q
        self.p = p
        self.f = f
        self.n_batch = n_batch
        self.optimizer = optimizer

        np.random.seed(random)
        self.srng = RandomStreams(seed=random)

        self.p_sample_mean_given_x()
        self.q_sample_mean_given_x()
        self.reconst()

        self.lowerbound()

    def iterate_lowerbound(self, x, mask, h, deterministic=False):
        # input
        # x : (batch_size, x_dim)
        # mask : (batch_size)
        # h : (batch_size, rnn_dim)

        prior_mean, prior_var = self.prior.fprop(
            [h],
            self.srng,
            deterministic=deterministic)
        q_mean, q_var = self.q.fprop(
            [x, h],
            self.srng,
            deterministic=deterministic)

        _KL = gauss_gauss_kl(q_mean, q_var, prior_mean, prior_var)
        KL = T.mean(_KL * mask)
        # z~q(z|x,h)
        z = self.q.sample_given_x(
            [x, h],
            self.srng,
            deterministic=deterministic)
        inverse_z = self.inverse_samples(z)
        # p(x|z,h)
        _loglike = self.p.log_likelihood_given_x(inverse_z)
        loglike = T.mean(_loglike[mask==1])

        h = self.f.fprop([x, z[-1], h], deterministic=deterministic)
        return h, kl, loglike

    def lowerbound(self):
        x = T.tensor3('x')
        x_dimshuffle = x.dimshuffle(1, 0, 2)
        mask = T.matrix('mask')
        mask_dimshuffle = mask.dimshuffle(1, 0)
        init_h = self.f.mean_network.get_hid_init(x.shape[0])

        [h_all, kl_all, loglike_all], scan_updates =\
            theano.scan(fn=self.iterate_lowerbound,
                        sequences=[x_dimshuffle, mask_dimshuffle],
                        outputs_info=[init_h, None, None])

        lowerbound = [-T.sum(kl_all), T.sum(loglike_all)]
        loss = -np.sum(lowerbound)

        f_params = self.f.get_params()
        prior_params = self.prior.get_params()
        q_params = self.q.get_params()
        p_params = self.p.get_params()
        params = f_params + prior_params + q_params + p_params

        # When there are sampling random numbers in the scan loop,
        # we must pass them as updates to theano.function.
        # http://theano-users.narkive.com/8DZmUJk7/recurrent-neural-network-theano-gradient-disconnectedinputerro
        updates = self.optimizer(loss, params) + scan_updates

        self.lowerbound_train = theano.function(
            inputs=[x, mask],
            outputs=lowerbound,
            updates=updates,
            on_unused_input='ignore')

    def train(self, train_set):
        n_x = train_set[0].shape[0]
        nbatches = n_x // self.n_batch
        lowerbound_train = []

        pbar = ProgressBar(maxval=nbatches).start()
        for i in range(nbatches):
            start = i * self.n_batch
            end = start + self.n_batch

            batch_x = [_x[start:end] for _x in train_set]
            train_L = self.lowerbound_train(*batch_x)
            lowerbound_train.append(np.array(train_L))
            pbar.update(i)
        lowerbound_train = np.mean(lowerbound_train, axis=0)

        return lowerbound_train

    def log_likelihood_test(self, test_set):
        x = T.tensor3('x')
        x_dimshuffle = x.dimshuffle(1, 0, 2)
        mask = T.matrix('mask')
        mask_dimshuffle = mask.dimshuffle(1, 0)
        init_h = self.f.mean_network.get_hid_init(x.shape[0])
        log_likelihood, updates = self.log_marginal_likelihood(x_dimshuffle, mask_dimshuffle, init_h)
        get_log_likelihood = theano.function(
            inputs=[x, mask],
            outputs=log_likelihood,
            updates=updates,
            on_unused_input='ignore')

        print "start sampling"

        n_x = test_set[0].shape[0]
        nbatches = n_x // self.n_batch

        pbar = ProgressBar(maxval=nbatches).start()
        all_log_likelihood = []
        for i in range(nbatches):
            start = i * self.n_batch
            end = start + self.n_batch
            batch_x = [_x[start:end] for _x in test_set]
            log_likelihood = get_log_likelihood(*batch_x)
            all_log_likelihood.append(np.array(log_likelihood))
            pbar.update(i)
        all_log_likelihood = np.mean(all_log_likelihood, axis=0)

        return all_log_likelihood

    def reconst(self):
        x = T.tensor3('x')
        x_dimshuffle = x.dimshuffle(1, 0, 2)
        init_h = self.f.mean_network.get_hid_init(x.shape[0])

        def iterate_sample(x, h):
            z = self.q.sample_mean_given_x(
                [x, h],
                self.srng,
                deterministic=True)
            inverse_z = self.inverse_samples(z)

            samples = self.p.sample_mean_given_x(
                inverse_z[0],
                self.srng,
                deterministic=True)

            h = self.f.fprop(
                [x, z[-1], h],
                deterministic=True)

            return h, samples[-1]

        [all_h, all_samples], scan_updates =\
            theano.scan(fn=iterate_sample,
                        sequences=[x_dimshuffle],
                        outputs_info=[init_h,None])

        all_samples_dimshuffle = all_samples.dimshuffle(1, 0, 2)
        self.reconst_x = theano.function(
            inputs=[x],
            outputs=all_samples_dimshuffle,
            updates=scan_updates,
            on_unused_input='ignore')

    def p_sample_mean_given_x(self):
        z = T.tensor3('z')
        z_dimshuffle = z.dimshuffle(1, 0, 2)
        init_h = self.f.mean_network.get_hid_init(z.shape[0])

        def iterate_p_sample(z, h):
            samples_mean = self.p.sample_mean_given_x(
                [z, h],
                self.srng,
                deterministic=True)
            samples = self.p.sample_given_x(
                [z, h],
                self.srng,
                deterministic=True)
            h = self.f.fprop(
                [samples[-1], z, h],
                deterministic=True)
            return h, samples[-1], samples_mean[-1]

        [all_h, all_samples, all_samples_mean], scan_updates =\
            theano.scan(fn=iterate_p_sample,
                        sequences=[z_dimshuffle],
                        outputs_info=[init_h, None, None])

        all_samples_dimshuffle = all_samples.dimshuffle(all_samples)
        all_samples_mean_dimshuffle = all_samples_mean.dimshuffle(all_samples)

        self.p_sample_mean_x = theano.function(
            inputs=[z],
            outputs=all_samples_mean_dimshuffle,
            updates=scan_updates,
            on_unused_input='ignore')

        self.p_sample_x = theano.function(
            inputs=[z],
            outputs=all_samples_dimshuffle,
            updates=scan_updates,
            on_unused_input='ignore')
        
    def q_sample_mean_given_x(self):
        x = T.tensor3('x')
        x_dimshuffle = x.dimshuffle(1, 0, 2)
        init_h = self.f.mean_network.get_hid_init(x.shape[0])

        def iterate_q_sample(x, h):
            samples_mean = self.q.sample_mean_given_x(
                [x, h],
                self.srng,
                deterministic=True)
            samples = self.q.sample_given_x(
                [x, h],
                self.srng,
                deterministic=True)
            h = self.f.fprop(
                [x, samples[-1], h],
                deterministic=True)
            return h, samples[-1], samples_mean[-1]

        [all_h, all_samples, all_samples_mean], scan_updates =\
            theano.scan(fn=iterate_q_sample,
                        sequences=[x_dimshuffle],
                        outputs_info=[init_h, None, None])

        all_samples_dimshuffle = all_samples.dimshuffle(all_samples)
        all_samples_mean_dimshuffle = all_samples_mean.dimshuffle(all_samples)

        self.q_sample_mean_x = theano.function(
            inputs=[x],
            outputs=all_samples_mean_dimshuffle,
            updates=scan_updates,
            on_unused_input='ignore')

        self.q_sample_x = theano.function(
            inputs=[x],
            outputs=all_samples_dimshuffle,
            updates=scan_updates,
            on_unused_input='ignore')

    def log_marginal_likelihood(self, x, mask, init_h):
        # TODO : deterministic=True
        [h_all, kl_all, loglike_all], scan_updates = \
            theano.scan(fn=self.iterate_lowerbound,
                        sequences=[x, mask],
                        outputs_info=[init_h, None, None])
        log_marginal_estimate = -T.sum(kl_all) + T.sum(loglike_all)
        return log_marginal_estimate, scan_updates

    def inverse_samples(self, samples):
        """
        inputs : [[x,y],z1,z2,...zn]
        outputs : [[zn,y],zn-1,...x]
        """
        inverse_samples = samples[::-1]
        inverse_samples[0] = [inverse_samples[0]] + inverse_samples[-1][1:]
        inverse_samples[-1] = inverse_samples[-1][0]
        return inverse_samples

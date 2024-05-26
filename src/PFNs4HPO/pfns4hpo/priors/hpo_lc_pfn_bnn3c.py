import os
import torch
import math
import numpy as np
import random
from scipy.stats import norm, beta, gamma, expon
from pfns4hpo.encoders import Normalize
from pfns4hpo.priors.utils import Batch
from pfns4hpo.priors import hebo_prior
from pfns4hpo.utils import default_device
from pfns4hpo import encoders


def progress_noise(X, sigma, L):
    EPS = 10**-9
    N = len(X)

    Z = np.random.normal(0,sigma,size=(N,))

    SIGMA = np.exp(-(np.subtract.outer(X, X)**2)/L)

    SIGMA += EPS*np.eye(N) # to guarantee SPD

    C = np.linalg.cholesky(SIGMA)

    return C @ Z

def add_noise_and_break(x, x_noise, Xsat, Rpsat):
    x = np.where(x < Xsat, x, Rpsat * (x - Xsat) + Xsat) # add a breaking point when saturation is reached
    noisy_x = x + x_noise
    # add the exponential tails to avoid negative x
    # TODO: actually make curve go to 0 in the negative range (would allow divergence beyond Y0)
    noisy_x = np.where(noisy_x > 1/1000, noisy_x, np.exp(noisy_x-1/1000+np.log(1/1000)))
    return noisy_x

def comb(x, Y0=0.2, Yinf=0.8, sigma=0.01, L=0.0001, PREC=[100]*4, Xsat=[1.0]*4, alpha=[np.exp(1), np.exp(-1), 1+np.exp(-4), np.exp(0)], Rpsat=[1.0]*4, w=[1/4]*4):
    x_noise = progress_noise(x,sigma,L)
    
    x_pow = add_noise_and_break(x, x_noise, Xsat[0], Rpsat[0])
    pow_x = (((PREC[0])**(1/alpha[0])-1)/Xsat[0]*x_pow + 1)**-alpha[0]
    
    x_exp = add_noise_and_break(x, x_noise, Xsat[1], Rpsat[1])
    exp_x = PREC[1]**(-(x_exp/Xsat[1])**alpha[1])

    x_log = add_noise_and_break(x, x_noise, Xsat[2], Rpsat[2])
    log_x = np.log(alpha[2])/(np.log((alpha[2]**PREC[2] - alpha[2]) * x_log/Xsat[2] + alpha[2]))

    x_hill = add_noise_and_break(x, x_noise, Xsat[3], Rpsat[3])
    hill_x = 1.0 / ((x_hill/Xsat[3])**alpha[3] * (PREC[3]-1) + 1)

    return Yinf - (Yinf - Y0)*(w[0]*pow_x + w[1]*exp_x+ w[2]*log_x+w[3]*hill_x)


class GaussianDeriv(torch.nn.Module): 
    def __init__(self): 
        super(GaussianDeriv, self).__init__() 
  
    def forward(self, x): 
        return -x * ((-x**2+1)/2).exp()


class DatasetPrior:

    output_sorted = None

    def init_weights(m):
        if type(m) == torch.nn.Linear:
            torch.nn.init.kaiming_normal_(m.weight)

    def _get_model(self):
        if self.hyperparams.get("bnn", "old") == "new":
            return torch.nn.Sequential(
                torch.nn.Linear(self.num_inputs+1, self.num_hidden, bias=False),
                GaussianDeriv(),
                torch.nn.Linear(self.num_hidden, self.num_hidden),
                torch.nn.Tanh(),
                torch.nn.Linear(self.num_hidden, self.num_hidden),
                torch.nn.Tanh(),
                torch.nn.Linear(self.num_hidden, self.num_outputs, bias=False)
            )
        else:
            model = torch.nn.Sequential(
                torch.nn.Linear(self.num_inputs, self.num_hidden),
                torch.nn.ELU(),
                torch.nn.Linear(self.num_hidden, self.num_hidden),
                torch.nn.ELU(),
                torch.nn.Linear(self.num_hidden, self.num_outputs)
            )
            if self.hyperparams.get("kaiming_init", True):
                model.apply(DatasetPrior.init_weights)
            return model
            

    def _output_for(self, input):
        with torch.no_grad():
            # normalize the inputs
            input = self.normalizer(input)
            # reweight the inputs for parameter importance
            if self.hyperparams.get("input_scaling", False):
                input = input*self.input_weights
            # apply the model produce the output
            output = self.model(input.float())
            # rescale and shift outputs to account for parameter sensitivity
            if self.hyperparams.get("output_scaling", False):
                output = output * self.output_sensitivity + self.output_offset 
            return output
        

    def __init__(self, num_params, num_outputs, hyperparams={}):
        self.num_features = num_params
        self.num_outputs = num_outputs
        self.hyperparams = hyperparams

        if self.hyperparams.get("input_subsampling", True):
            self.num_inputs = 2*(num_params+2)
        else:
            self.num_inputs = num_params+1
        self.num_hidden = self.hyperparams.get("num_hidden", 100)

        N_datasets = self.hyperparams.get("N_datasets", 1000)
        N_per_dataset = self.hyperparams.get("N_per_dataset", 1)

        self.normalizer = Normalize(0.5, math.sqrt(1 / 12))

        if DatasetPrior.output_sorted is None:
            # generate 1M samples to approximate the CDF of the BNN output distribution
            output = torch.zeros((N_datasets, N_per_dataset, num_outputs))
            input = torch.from_numpy(np.random.uniform(size=(N_datasets, N_per_dataset, self.num_inputs))).to(torch.float32)
            if not self.hyperparams.get("input_subsampling", True):
                input = torch.cat((input, torch.ones((N_datasets, N_per_dataset, 1))), -1)  # add ones as input bias 
            with torch.no_grad():
                for i in range(N_datasets):
                    if i % 100 == 99:
                        print(f"{i+1}/{N_datasets}")
                    # sample a new dataset
                    self.new_dataset()
                    for j in range(N_per_dataset):
                        output_ij = self._output_for(input[i,j])
                        output[i,j,:] = output_ij
                DatasetPrior.output_sorted = np.sort(torch.flatten(output).numpy())

        self.new_dataset()

    
    def new_dataset(self):
        # reinitialize all dataset specific random variables
        # reinit the parameters of the BNN
        self.model = self._get_model()
        # initial performance (after init)
        self.y0 = np.random.uniform()
        # the input weights (parameter importance & magnitude of aleatoric uncertainty on the curve)
        if self.hyperparams.get("input_scaling", False):
            param_importance = np.random.dirichlet([1]*(self.num_inputs-1) + [0.1]) # relative parameter importance
            lscale = np.exp(np.random.normal(2, 0.5)) # length scale ~ complexity of the landscape
            self.input_weights = np.concatenate((param_importance*lscale*self.num_inputs, np.full((1,),lscale)), axis=0)
        # the output weights (curve property sensitivity)
        if self.hyperparams.get("output_scaling", False):
            self.output_sensitivity = np.random.uniform(size=(self.num_outputs,))
            self.output_offset = np.random.uniform((self.output_sensitivity-1)/2,(1-self.output_sensitivity)/2)
        # subsample inputs (alternative to input weighing)
        if self.hyperparams.get("input_subsampling", True):
            self.input_features = np.random.uniform(size=(self.num_inputs,))
            p_alloc = np.random.dirichlet(tuple([1 for _ in range(self.num_features)] + [1, 1]))
            self.alloc = np.random.choice(self.num_features+2, size=(self.num_inputs,), p=p_alloc)

    def curves_for_configs(self, configs, noise=True):
        # more efficient batch-wise
        bnn_outputs = self.output_for_config(configs, noise=noise)
        indices = np.searchsorted(DatasetPrior.output_sorted, bnn_outputs, side='left')
        rng4config = MyRNG(indices)
        
        if self.hyperparams.get("pow3", True):
            c = rng4config.uniform(a=self.y0, b=1.0)
            a = c - self.y0
            alpha = np.exp(rng4config.normal(scale=2))
            sigma = np.exp(rng4config.normal(loc=-5, scale=1))
        
            def foo(x_,cid=0):
                # x is a number from 0 to 1
                y_ = c[cid] - a[cid] * np.power(50*x_+1, -alpha[cid])
                noise = np.random.normal(size=y_.shape, scale=sigma[cid])
                return y_ + noise
                    
            return foo
        else:
            # more efficient batch-wise
            ncurves = 4

    
            Y0 = self.y0
        
            # sample Yinf (shared by all components)
            Yinf = 1 - rng4config.uniform(a=0, b=1-Y0)  # 0
            
            
            # sample weights for basis curves (dirichlet)
            w = np.stack([rng4config.gamma(a=1) for i in range(ncurves)]).T # 1, 2, 3, 4
            w = w/w.sum(axis=1,keepdims=1)
            
        
            # sample shape/skew parameter for each basis curve
            alpha = np.stack([np.exp(rng4config.normal(1,1)), # 5
                     np.exp(rng4config.normal(0,1)), # 6
                     1.0+np.exp(rng4config.normal(-4,1)), # 7
                     np.exp(rng4config.normal(0.5,0.5))]).T # 8
            
        
            # sample saturation x for each basis curve
            Xsat_max = 10**rng4config.normal(0,1)  # max saturation # 9
            
            Xsat_rel = np.stack([rng4config.gamma(a=1) for i in range(ncurves)]).T # relative saturation points # 10, 11, 12, 13
            
            Xsat = ((Xsat_max.T * Xsat_rel.T) / np.max(Xsat_rel,axis=1)).T
            
            #Xsat = [Xsat_max, Xsat_max, Xsat_max, Xsat_max]
            
            # sample relative saturation y (PREC) for each basis curve
            PREC = np.stack([1.0 / 10**rng4config.uniform(-3,0) for i in range(ncurves)]).T # 14, 15, 16, 17
            # post saturation convergence/divergence rate for each basis curve
            Rpsat = np.stack([1.0 - rng4config.exponential(scale=1) for i in range(ncurves)]).T # 18, 19, 20, 21
        
            # sample noise parameters
            sigma = np.exp(rng4config.normal(-3.5,0.5)) # STD of the xGP 22
            
            L = 10**rng4config.normal(-4,1) # Length-scale of the xGP 23
            
            def foo(x_, cid=0):
                y_ = comb(x_, Y0=Y0, Yinf=Yinf[cid], sigma=sigma[cid], L=L[cid], Xsat=Xsat[cid], alpha=alpha[cid], Rpsat=Rpsat[cid], w=w[cid])
                return y_
                    
            return foo


    def output_for_config(self, config, noise=True):
        # subsample the configuration
        if self.hyperparams.get("input_subsampling", True):
            input_noise = np.random.uniform(size=(*config.shape[:-1], self.num_inputs)) if noise else 0.5
            input = np.zeros((*config.shape[:-1], self.num_inputs))
            for j in range(self.num_inputs):
                if self.alloc[j] < self.num_features:
                    input[...,j] = config[...,self.alloc[j]]
                elif self.alloc[j] == self.num_features:
                    input[...,j] = self.input_features[j]
                else:
                    input[...,j] = input_noise[...,j]
        else:
            # add aleatoric noise & bias
            input = np.concatenate((config, np.random.uniform(size=(*config.shape[:-1],1)) if noise else 0.5, np.ones((*config.shape[:-1],1))), -1)
        output = self._output_for(torch.from_numpy(input))
        return output.numpy()

    def uniform(self, bnn_output, a=0.0, b=1.0):
        indices = np.searchsorted(DatasetPrior.output_sorted, bnn_output, side='left')
        return (b-a) * indices / len(DatasetPrior.output_sorted) + a
    
    def normal(self, bnn_output, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(bnn_output, a=eps, b=1-eps)
        return norm.ppf(u, loc=loc, scale=scale)

    def beta(self, bnn_output, a=1, b=1, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(bnn_output, a=eps, b=1-eps)
        return beta.ppf(u, a=a, b=b, loc=loc, scale=scale)

    def gamma(self, bnn_output, a=1, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(bnn_output, a=eps, b=1-eps)
        return gamma.ppf(u, a=a, loc=loc, scale=scale)

    def exponential(self, bnn_output, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(bnn_output, a=eps, b=1-eps)
        return expon.ppf(u, scale=scale)

class MyRNG:

    def __init__(self, indices):
        self.indices = indices.T
        self.reset()

    def reset(self):
        self.counter = 0

    def uniform(self, a=0.0, b=1.0):
        u = (b-a) * self.indices[self.counter] / len(DatasetPrior.output_sorted) + a
        self.counter += 1
        return u
    
    def normal(self, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(a=eps, b=1-eps)
        return norm.ppf(u, loc=loc, scale=scale)

    def beta(self, a=1, b=1, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(a=eps, b=1-eps)
        return beta.ppf(u, a=a, b=b, loc=loc, scale=scale)

    def gamma(self, a=1, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(a=eps, b=1-eps)
        return gamma.ppf(u, a=a, loc=loc, scale=scale)

    def exponential(self, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(a=eps, b=1-eps)
        return expon.ppf(u, scale=scale)


def curve_prior(dataset, config):
    # calls the more efficient batch-wise method
    return dataset.curves_for_configs(np.array([config]))
   


# function producing batches for PFN training
@torch.no_grad()
def get_batch(
    batch_size,
    seq_len,
    num_features,
    single_eval_pos,
    device=default_device,
    hyperparameters=None,
    **kwargs,
):

    # assert num_features == 2
    assert num_features >= 2
    EPS = 10**-9
    
    num_params = np.random.randint(1, num_features - 1) # beware upper bound is exclusive!

    if hyperparameters.get("pow3", True):
        dataset_prior = DatasetPrior(num_params, 3, hyperparameters)
    else:
        dataset_prior = DatasetPrior(num_params, 24, hyperparameters)
    
    x = []
    y = []

    for i in range(batch_size):
        epoch = torch.zeros(seq_len)
        id_curve = torch.zeros(seq_len)
        curve_val = torch.zeros(seq_len)
        config = torch.zeros(seq_len, num_params)

        # determine the number of fidelity levels (ranging from 1: BB, up to seq_len)
        n_levels = int(np.round(10**np.random.uniform(0,3)))
        #print(f"n_levels: {n_levels}")

        # determine # observations/queries per curve
        # TODO: also make this a dirichlet thing
        alpha = 10**np.random.uniform(-4,-1)
        #print(f"alpha: {alpha}")
        weights = np.random.gamma(alpha,alpha,seq_len)+EPS
        p = weights / np.sum(weights)
        ids = np.arange(seq_len)
        all_levels = np.repeat(ids, n_levels)
        all_p = np.repeat(p, n_levels)/n_levels
        ordering = np.random.choice(all_levels, p=all_p, size=seq_len, replace=False)

        # calculate the cutoff/samples for each curve
        cutoff_per_curve = np.zeros((seq_len,), dtype=int)
        epochs_per_curve = np.zeros((seq_len,), dtype=int)
        for i in range(seq_len): # loop over every pos
            cid = ordering[i]
            epochs_per_curve[cid] += 1
            if i < single_eval_pos:
                cutoff_per_curve[cid] += 1

        # fix dataset specific random variables
        dataset_prior.new_dataset()

        # determine config, x, y for every curve
        curve_configs = np.random.uniform(size=(seq_len, num_params))
        curves = dataset_prior.curves_for_configs(curve_configs)
        curve_xs = []
        curve_ys = []
        for cid in range(seq_len): # loop over every curve
            if epochs_per_curve[cid] > 0:
                # determine x (observations + query)
                x_ = np.zeros((epochs_per_curve[cid],))
                if cutoff_per_curve[cid] > 0: # observations (if any)
                    x_[:cutoff_per_curve[cid]] = np.arange(1,cutoff_per_curve[cid]+1)/n_levels
                if cutoff_per_curve[cid] < epochs_per_curve[cid]: # queries (if any)
                    x_[cutoff_per_curve[cid]:] = np.random.choice(np.arange(cutoff_per_curve[cid]+1, n_levels+1),
                                                                 size=epochs_per_curve[cid]-cutoff_per_curve[cid],
                                                                 replace=False)/n_levels
                curve_xs.append(x_)
                # determine y's
                y_ = curves(torch.from_numpy(x_), cid)
                curve_ys.append(y_)
            else:
                curve_xs.append(None)
                curve_ys.append(None)

        # construct the batch data element
        curve_counters = torch.zeros(seq_len).type(torch.int64)
        for i in range(seq_len):
            cid = ordering[i]
            if i < single_eval_pos or curve_counters[cid] > 0:
                id_curve[i] = cid + 1  # reserve ID 0 for queries
            else:
                id_curve[i] = 0  # queries for unseen curves always have ID 0
            epoch[i] = curve_xs[cid][curve_counters[cid]]
            config[i] = torch.from_numpy(curve_configs[cid])
            curve_val[i] = curve_ys[cid][curve_counters[cid]]
            curve_counters[cid] += 1 
           
        x.append(torch.cat([torch.stack([id_curve, epoch], dim=1), config], dim=1))
        y.append(curve_val)

    x = torch.stack(x, dim=1).to(device).float()
    y = torch.stack(y, dim=1).to(device).float()

    return Batch(x=x, y=y, target_y=y)


class MultiCurvesEncoder(torch.nn.Module):
    def __init__(self, in_dim, out_dim):
        super().__init__()
        seq_len = 1000
        self.normalizer = torch.nn.Sequential(
            encoders.Normalize(0.5, math.sqrt(1 / 12)),
        )
        self.epoch_enc = torch.nn.Linear(1, out_dim, bias=False)
        self.idcurve_enc = torch.nn.Embedding(seq_len+1, out_dim)
        self.configuration_enc = encoders.get_variable_num_features_encoder(encoders.Linear)(in_dim-2, out_dim)

    def forward(self, *x, **kwargs):
        x = torch.cat(x, dim=-1)
        out = self.epoch_enc(self.normalizer(x[..., 1:2])) \
            + self.idcurve_enc(x[..., :1].int()).squeeze(2) \
            + self.configuration_enc(x[..., 2:])
        return out


def get_encoder():
    return lambda num_features, emsize: MultiCurvesEncoder(num_features, emsize)
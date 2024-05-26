import os
import torch
import math
import numpy as np
import random
from scipy.stats import norm
from pfns4hpo.priors.utils import Batch
from pfns4hpo.priors import hebo_prior
from pfns4hpo.utils import default_device
from pfns4hpo import encoders

def pow3(x, a, c, alpha, *args):
    return c - a * torch.pow(x+1, -alpha)


class DatasetPrior:

    output_sorted = None

    def init_weights(m):
        if type(m) == torch.nn.Linear:
            torch.nn.init.kaiming_normal_(m.weight)

    def __init__(self, num_features, num_outputs):
        self.num_features = num_features
        self.num_outputs = num_outputs
        
        self.num_inputs = 2*(num_features+2)
        num_hidden = 100
        N = 1000
        
        self.model = torch.nn.Sequential(
            encoders.Normalize(0.5, math.sqrt(1 / 12)),
            torch.nn.Linear(self.num_inputs, num_hidden),
            torch.nn.ELU(),
            torch.nn.Linear(num_hidden, num_hidden),
            torch.nn.ELU(),
            torch.nn.Linear(num_hidden, self.num_outputs)
        )

        if DatasetPrior.output_sorted is None:
            # generate samples to approximate the CDF of the BNN output distribution
            output = torch.zeros((N, num_outputs))
            input = torch.from_numpy(np.random.uniform(size=(N,self.num_inputs))).to(torch.float32)
            with torch.no_grad():
                for i in range(N):
                    self.model.apply(DatasetPrior.init_weights)
                    output[i] = self.model(input[i])
            
            DatasetPrior.output_sorted = np.sort(torch.flatten(output).numpy())

        # fix the parameters of the BNN
        self.model.apply(DatasetPrior.init_weights)

        # fix other dataset specific
        self.input_features = np.random.uniform(size=(self.num_inputs,))
        p_alloc = np.random.dirichlet(tuple([1 for _ in range(self.num_features)] + [1, 1]))
        self.alloc = np.random.choice(self.num_features+2, size=(self.num_inputs,), p=p_alloc)
        #print(self.alloc)

    
    def input_for_config(self, config):
        input_noise = np.random.uniform(size=(self.num_inputs,))
        input = torch.zeros((self.num_inputs,))
        for j in range(self.num_inputs):
            if self.alloc[j] < self.num_features:
                input[j] = config[self.alloc[j]]
            elif self.alloc[j] == self.num_features:
                input[j] = self.input_features[j]
            else:
                input[j] = input_noise[j]
        return input
        
    def output_for_config(self, config):
        input = self.input_for_config(config)
        return self.model(input)

    def uniform(self, bnn_output, a=0.0, b=1.0):
        indices = np.searchsorted(DatasetPrior.output_sorted, bnn_output, side='left')
        return (b-a) * indices / len(DatasetPrior.output_sorted) + a
    
    def normal(self, bnn_output, loc=0, scale=1):
        eps = 0.5 / len(DatasetPrior.output_sorted) # to avoid infinite samples
        u = self.uniform(bnn_output, a=eps, b=1-eps)
        return norm.ppf(u, loc=loc, scale=scale)


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
    if hyperparameters.get("load_path", False):
        if not hasattr(get_batch, "seq_counter"):
            get_batch.seq_counter = 0
            get_batch.loaded_chunk_id = None
            get_batch.loaded_chunk_x = None
            get_batch.loaded_chunk_y = None
            
        path = hyperparameters["load_path"]
        chunk_size = hyperparameters["chunk_size"]
        n_chunks = hyperparameters["n_chunks"]
        
        chunk_id = get_batch.seq_counter // chunk_size
        chunk_id = chunk_id % n_chunks  # cycle through the data
        if chunk_id != get_batch.loaded_chunk_id:
            # we need to load the next chunk
            get_batch.loaded_chunk_x = np.load(os.path.join(path, f"chunk_{chunk_id}_x.npy"))
            get_batch.loaded_chunk_y = np.load(os.path.join(path, f"chunk_{chunk_id}_y.npy"))
            get_batch.loaded_chunk_id = chunk_id
            x = get_batch.loaded_chunk_x[:,:batch_size]
            y = get_batch.loaded_chunk_y[:,:batch_size]
        else:
            offset = get_batch.seq_counter % chunk_size
            if offset+batch_size <= chunk_size:
                # we have all the data needed in memory
                x = get_batch.loaded_chunk_x[:,offset:offset+batch_size]
                y = get_batch.loaded_chunk_y[:,offset:offset+batch_size]
            else:
                # we have part of the data needed in memory, eagerly load next chunk already
                next_chunk_id = (chunk_id+1) % n_chunks
                next_chunk_x = np.load(os.path.join(path, f"chunk_{next_chunk_id}_x.npy"))
                next_chunk_y = np.load(os.path.join(path, f"chunk_{next_chunk_id}_y.npy"))
                # load rest
                x = np.concatenate((get_batch.loaded_chunk_x[:,offset:],
                                    next_chunk_x[:,:batch_size-chunk_size+offset]), axis=1)
                y = np.concatenate((get_batch.loaded_chunk_y[:,offset:],
                                    next_chunk_y[:,:batch_size-chunk_size+offset]), axis=1)
                get_batch.loaded_chunk_x = next_chunk_x
                get_batch.loaded_chunk_y = next_chunk_y
                get_batch.loaded_chunk_id = next_chunk_id
        assert(len(x[0]) == batch_size)
        assert(len(y[0]) == batch_size)
        get_batch.seq_counter += batch_size
        
        x = torch.from_numpy(x).to(device).float()
        y = torch.from_numpy(y).to(device).float()
    
        return Batch(x=x, y=y, target_y=y)
    else:
        # assert num_features == 2
        ncurves = hyperparameters.get("ncurves", 50)
        nepochs = hyperparameters.get("nepochs", 50)
    
        assert seq_len == ncurves * nepochs
        assert num_features >= 2

        if hyperparameters.get("fix_nparams", False):
            num_params = num_features - 2
        else:
            num_params = np.random.randint(1, num_features - 2)

        x_ = torch.arange(1, nepochs + 1)
    
        x = []
        y = []
    
        for i in range(batch_size):
            epoch = torch.zeros(nepochs * ncurves)
            id_curve = torch.zeros(nepochs * ncurves)
            curve_val = torch.zeros(nepochs * ncurves)
            config = torch.zeros(nepochs * ncurves, num_params)
    
            # sample a collection of curves
            dataset = DatasetPrior(num_params, 3)
            curves = []
            curve_configs = []
            c_minus_a = np.random.uniform()
            for i in range(ncurves):
                c = np.random.uniform(size=(num_params,))  # random configurations
                curve_configs.append(c)
                output = dataset.output_for_config(c).numpy()
                c = dataset.uniform(output[0], a=c_minus_a, b=1.0)
                a = c - c_minus_a
                alpha = np.exp(dataset.normal(output[1], scale=2))
                sigma = np.exp(dataset.normal(output[2], loc=-5, scale=1))
                y_ = pow3(x_, a, c, alpha)
                noise = np.random.normal(size=x_.shape, scale=sigma)
                y_noisy = y_+noise
                curves.append(y_noisy)
                if torch.isnan(torch.sum(y_noisy)) or not torch.isfinite(torch.sum(y_noisy)):
                    print(f"{a}, {c}, {alpha}, {sigma}")
                    print(y_+noise)
    
            # determine an ordering
            p_new = 10**np.random.uniform(-3, 0)
            greediness = 10**np.random.uniform(0, 3)  # Select prob 'best' / 'worst' possible (1 = URS)
            ordering = hyperparameters.get("ordering", "URS")
            ids = np.arange(ncurves)
            np.random.shuffle(ids)  # randomize the order of IDs to avoid learning weird biases
            cutoff = torch.zeros(ncurves).type(torch.int64)
            for i in range(ncurves * nepochs):
                if ordering == "URS":
                    candidates = [j for j, c in enumerate(cutoff) if c < nepochs]
                    selected = np.random.choice(candidates)
                elif ordering == "BFS":
                    selected = i % ncurves
                elif ordering == "DFS":
                    selected = i // nepochs
                elif ordering == "SoftGreedy":
                    u = np.random.uniform()
                    if u < p_new:
                        new_candidates = [j for j, c in enumerate(cutoff) if c == 0]
                    if u < p_new and len(new_candidates) > 0:
                        selected = np.random.choice(new_candidates)
                    else:
                        candidates = [j for j, c in enumerate(cutoff) if c < nepochs and c > 0]
                        if len(candidates) == 0:
                            if u >= p_new:
                                new_candidates = [j for j, c in enumerate(cutoff) if c == 0]
                            selected = np.random.choice(new_candidates)
                        else:
                            # use softmax selection based on performance and selected cutoff
                            # select a cutoff to compare on (based on current / last)
                            selected_cutoff = np.random.randint(nepochs)
                            values = []
                            for j in candidates:
                                values.append(curves[j][min(selected_cutoff,cutoff[j]-1)])
                            sm_values = np.power(greediness, np.asarray(values))
                            sm_values = sm_values / np.sum(sm_values)
                            if np.isnan(np.sum(sm_values)):
                                print(values)
                                print(greediness)
                                print(selected_cutoff)
                                print(cutoff[j])
                                for j in candidates:
                                    print(curves[:cutoff[j]])
                            selected = np.random.choice(candidates, p=sm_values)
                else:
                    raise NotImplementedError
                id_curve[i] = ids[selected]
                curve_val[i] = curves[selected][cutoff[selected]]
                config[i] = torch.from_numpy(curve_configs[selected])
                cutoff[selected] += 1
                epoch[i] = cutoff[selected]
            x.append(torch.cat([torch.stack([id_curve, epoch], dim=1), config], dim=1))
            y.append(curve_val)
    
        x = torch.stack(x, dim=1).to(device).float()
        y = torch.stack(y, dim=1).to(device).float()
    
        return Batch(x=x, y=y, target_y=y)


class MultiCurvesEncoder(torch.nn.Module):
    def __init__(self, in_dim, out_dim):
        super().__init__()
        self.normalizer = torch.nn.Sequential(
            encoders.Normalize(0.0, 50.0),
            encoders.Normalize(0.5, math.sqrt(1 / 12)),
        )
        self.epoch_enc = torch.nn.Linear(1, out_dim, bias=False)
        self.idcurve_enc = torch.nn.Embedding(51, out_dim)
        self.configuration_enc = encoders.get_variable_num_features_encoder(encoders.Linear)(in_dim-2, out_dim)

    def forward(self, *x, **kwargs):
        x = torch.cat(x, dim=-1)
        out = self.epoch_enc(self.normalizer(x[..., 1:2])) \
            + self.idcurve_enc(x[..., :1].int()).squeeze(2) \
            + self.configuration_enc(x[..., 2:])
        return out


def get_encoder():
    return lambda num_features, emsize: MultiCurvesEncoder(num_features, emsize)
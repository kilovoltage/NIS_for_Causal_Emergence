import torch
from torch import nn
from torch import distributions
from torch.nn.parameter import Parameter
from EI_calculation import approx_ei
class InvertibleNN(nn.Module):
    def __init__(self, nets, nett, mask, device):
        super(InvertibleNN, self).__init__()
        
        self.device = device
        self.mask = nn.Parameter(mask, requires_grad=False)
        length = mask.size()[0] // 2
        self.t = torch.nn.ModuleList([nett() for _ in range(length)]) #repeating len(masks) times
        self.s = torch.nn.ModuleList([nets() for _ in range(length)])
        self.size = mask.size()[1]
    def g(self, z):
        x = z
        log_det_J = x.new_zeros(x.shape[0], device=self.device)
        for i in range(len(self.t)):
            x_ = x*self.mask[i]
            s = self.s[i](x_)*(1 - self.mask[i])
            t = self.t[i](x_)*(1 - self.mask[i])
            x = x_ + (1 - self.mask[i]) * (x * torch.exp(s) + t)
            log_det_J += s.sum(dim=1)
        return x, log_det_J

    def f(self, x):
        log_det_J, z = x.new_zeros(x.shape[0], device=self.device), x
        for i in reversed(range(len(self.t))):
            z_ = self.mask[i] * z
            s = self.s[i](z_) * (1-self.mask[i])
            t = self.t[i](z_) * (1-self.mask[i])
            z = (1 - self.mask[i]) * (z - t) * torch.exp(-s) + z_
            log_det_J -= s.sum(dim=1)
        return z, log_det_J
class Renorm_Dynamic(nn.Module):
    def __init__(self, sym_size, latent_size, effect_size, hidden_units,normalized_state,device,is_random=False):
        #latent_size: input size
        #effect_size: scale, effective latent dynamics size
        super(Renorm_Dynamic, self).__init__()
        if sym_size % 2 !=0:
            sym_size = sym_size + 1
        self.device = device
        self.latent_size = latent_size
        self.effect_size = effect_size
        self.sym_size = sym_size
        nets = lambda: nn.Sequential(nn.Linear(sym_size, hidden_units), nn.LeakyReLU(), 
                                     nn.Linear(hidden_units, hidden_units), nn.LeakyReLU(), 
                                     nn.Linear(hidden_units, sym_size), nn.Tanh())
        nett = lambda: nn.Sequential(nn.Linear(sym_size, hidden_units), nn.LeakyReLU(), 
                                     nn.Linear(hidden_units, hidden_units), nn.LeakyReLU(), 
                                     nn.Linear(hidden_units, sym_size))
        self.dynamics = nn.Sequential(nn.Linear(latent_size, hidden_units), nn.LeakyReLU(), 
                                 nn.Linear(hidden_units, hidden_units), nn.LeakyReLU(), 
                                 nn.Linear(hidden_units, latent_size))
        self.inv_dynamics = nn.Sequential(nn.Linear(latent_size, hidden_units), nn.LeakyReLU(), 
                                 nn.Linear(hidden_units, hidden_units), nn.LeakyReLU(), 
                                 nn.Linear(hidden_units, latent_size))
        mask1 = torch.cat((torch.zeros(1, sym_size // 2, device=self.device), torch.ones(1, sym_size // 2, device=self.device)), 1)
        mask2 = 1 - mask1
        masks = torch.cat((mask1, mask2, mask1, mask2, mask1, mask2), 0)
        
        prior = distributions.MultivariateNormal(torch.zeros(latent_size), torch.eye(latent_size))
        self.flow = InvertibleNN(nets, nett, masks, self.device)
        self.normalized_state=normalized_state
        self.is_random = is_random
        if is_random:
            self.sigmas = torch.nn.parameter.Parameter(torch.rand(1, latent_size, device=self.device))
    def forward(self, x):
        #state_dim = x.size()[1]
        if len(x.size())<=1:
            x = x.unsqueeze(0)
        
        s = self.encoding(x)
        s_next = self.dynamics(s) + s
        if self.normalized_state:
            s_next = torch.tanh(s_next)
        if self.is_random:
            s_next = s_next + torch.relu(self.sigmas.repeat(s_next.size()[0],1)) * torch.randn(s_next.size(), device=self.device)
        y = self.decoding(s_next)
        return y, s, s_next
    def back_forward(self, x):
        #state_dim = x.size()[1]
        if len(x.size())<=1:
            x = x.unsqueeze(0)
        
        s = self.encoding(x)
        s_next = self.inv_dynamics(s) - s
        if self.normalized_state:
            s_next = torch.tanh(s_next)
        if self.is_random:
            s_next = s_next + torch.relu(self.sigmas.repeat(s_next.size()[0],1)) * torch.randn(s_next.size(), device=self.device)
        y = self.decoding(s_next)
        return y, s, s_next
    def multi_step_forward(self, x, steps):
        batch_size = x.size()[0]
        x_hist = x
        predict, latent, latent_n = self.forward(x)
        z_hist = latent
        n_hist = torch.zeros(x.size()[0], x.size()[1]-latent.size()[1], device = self.device)
        for t in range(steps):    
            z_next, x_next, noise = self.simulate(latent)
            z_hist = torch.cat((z_hist, z_next), 0)
            x_hist = torch.cat((x_hist, self.eff_predict(x_next)), 0)
            n_hist = torch.cat((n_hist, noise), 0)
            latent = z_next
        return x_hist[batch_size:,:], z_hist[batch_size:,:], n_hist[batch_size:,:]
    def decoding(self, s_next):
        sz = self.sym_size - self.latent_size
        if sz>0:
            noise = distributions.MultivariateNormal(torch.zeros(sz), torch.eye(sz)).sample((s_next.size()[0], 1))
            noise = noise.to(self.device)
            #print(noise.size(), s_next.size(1))
            if s_next.size()[0]>1:
                noise = noise.squeeze(1)
            else:
                noise = noise.squeeze(0)
            #print(noise.size())
            zz = torch.cat((s_next, noise), 1)
        else:
            zz = s_next
        y,_ = self.flow.g(zz)
        return y
    def decoding1(self, s_next):
        sz = self.sym_size - self.latent_size
        if sz>0:
            noise = distributions.MultivariateNormal(torch.zeros(sz), torch.eye(sz)).sample((s_next.size()[0], 1))
            noise = noise.to(self.device)
            #print(noise.size(), s_next.size(1))
            if s_next.size()[0]>1:
                noise = noise.squeeze(1)
            else:
                noise = noise.squeeze(0)
            #print(noise.size())
            zz = torch.cat((s_next, noise), 1)
        else:
            noise = distributions.MultivariateNormal(torch.zeros(sz), torch.eye(sz)).sample((s_next.size()[0], 1))
            noise = noise.to(self.device)
            #print(noise.size(), s_next.size(1))
            if s_next.size()[0]>1:
                noise = noise.squeeze(1)
            else:
                noise = noise.squeeze(0)
            zz = s_next
        y,_ = self.flow.g(zz)
        return y, noise
    def encoding(self, x):
        xx = x
        if len(x.size()) > 1:
            if x.size()[1] < self.sym_size:
                xx = torch.cat((x, torch.zeros([x.size()[0], self.sym_size - x.size()[1]], device=self.device)), 1)
        else:
            if x.size()[0] < self.sym_size:
                xx = torch.cat((x, torch.zeros([self.sym_size - x.size()[0]], device=self.device)), 0)
        s, _ = self.flow.f(xx)
        if self.normalized_state:
            s = torch.tanh(s)
        return s[:, :self.latent_size]
    def encoding1(self, x):
        xx = x
        if len(x.size()) > 1:
            if x.size()[1] < self.sym_size:
                xx = torch.cat((x, torch.zeros([x.size()[0], self.sym_size - x.size()[1]], device=self.device)), 1)
        else:
            if x.size()[0] < self.sym_size:
                xx = torch.cat((x, torch.zeros([self.sym_size - x.size()[0]], device=self.device)), 0)
        s, _ = self.flow.f(xx)
        if self.normalized_state:
            s = torch.tanh(s)
        return s[:, :self.latent_size], s[:,self.latent_size:]
    def eff_predict(self, prediction):
        return prediction[:, :self.effect_size]
    def simulate(self, x):
        x_next = self.dynamics(x) + x
        if self.normalized_state:
            x_next = torch.tanh(x_next)
        if self.is_random:
            x_next = x_next + torch.relu(self.sigmas.repeat(x_next.size()[0],1)) * torch.randn(x_next.size(), device=self.device)
        decode,noise = self.decoding1(x_next)
        return x_next, decode, noise
    def multi_step_prediction(self, s, steps):
        s_hist = s
        z_hist = self.encoding(s)
        z = z_hist[:1, :]
        for t in range(steps):    
            z_next, s_next, _ = self.simulate(z)
            z_hist = torch.cat((z_hist, z_next), 0)
            s_hist = torch.cat((s_hist, self.eff_predict(s_next)), 0)
            z = z_next
        return s_hist, z_hist


# Copyright (c) 2024 Wenyuan Zhao, Haoyuan Chen
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# @authors: Wenyuan Zhao, Haoyuan Chen.
#
# ===============================================================================================


from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F

from dmgp.layers import LinearReparameterization, Conv2dReparameterization
from dmgp.layers import AMK

__all__ = ['DMGPcifar']

prior_mu = 0.0
prior_sigma = 1.0
posterior_mu_init = 0.0
posterior_rho_init = -3.0


class DMGPcifar(nn.Module):
    def __init__(self,
                 input_dim,
                 output_dim,
                 design_class,
                 kernel,
                 option='additive'):
        super(DMGPcifar, self).__init__()

        self.option = option

        self.conv1 = Conv2dReparameterization(
            in_channels=3,
            out_channels=32,
            kernel_size=3,
            stride=1,
            prior_mean=prior_mu,
            prior_variance=prior_sigma,
            posterior_mu_init=posterior_mu_init,
            posterior_rho_init=posterior_rho_init,
        )

        self.conv2 = Conv2dReparameterization(
            in_channels=32,
            out_channels=64,
            kernel_size=3,
            stride=1,
            prior_mean=prior_mu,
            prior_variance=prior_sigma,
            posterior_mu_init=posterior_mu_init,
            posterior_rho_init=posterior_rho_init,
        )
        self.dropout1 = nn.Dropout2d(0.25)
        self.dropout2 = nn.Dropout2d(0.5)

        w0 = 128
        self.fc0 = LinearReparameterization(
            in_features=12544,
            out_features=w0,
            prior_mean=prior_mu,
            prior_variance=prior_sigma,
            posterior_mu_init=posterior_mu_init,
            posterior_rho_init=posterior_rho_init,
            bias=True,
        )

        #################################################################################
        ## 1st layer of DGP: input:[n, input_dim] size tensor, output:[n, w1] size tensor
        #################################################################################
        # return [n, m1] size tensor for [n, input_dim] size input and [m1, input_dim] size sparse grid
        self.gp1 = AMK(in_features=w0, n_level=5, design_class=design_class, kernel=kernel)
        m1 = self.gp1.out_features # m1 = input_dim*(2^n_level-1)
        w1 = 128
        # return [n, w1] size tensor for [n, m1] size input and [m1, w1] size weights
        self.fc1 = LinearReparameterization(
            in_features=m1,
            out_features=w1,
            prior_mean=prior_mu,
            prior_variance=prior_sigma,
            posterior_mu_init=posterior_mu_init,
            posterior_rho_init=posterior_rho_init,
            bias=True,
        )

        #################################################################################
        ## 2nd layer of DGP: input:[n, w1] size tensor, output:[n, w2] size tensor
        #################################################################################
        # return [n, m2] size tensor for [n, w1] size input and [m2, w1] size sparse grid
        self.gp2 = AMK(in_features=w1, n_level=5, design_class=design_class, kernel=kernel)
        m2 = self.gp2.out_features # m2 = w1*(2^n_level-1)
        w2 = output_dim
        # return [n, w2] size tensor for [n, m2] size input and [m2, w2] size weights
        self.fc2 = LinearReparameterization(
            in_features=m2,
            out_features=w2,
            prior_mean=prior_mu,
            prior_variance=prior_sigma,
            posterior_mu_init=posterior_mu_init,
            posterior_rho_init=posterior_rho_init,
            bias=True,
        )

    def forward(self, x):
        kl_sum = 0
        x, kl = self.conv1(x)
        kl_sum += kl
        x = F.relu(x)
        x, kl = self.conv2(x)
        kl_sum += kl
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x, kl = self.fc0(x)
        kl_sum += kl

        x = self.gp1(x)
        x, kl = self.fc1(x)
        kl_sum += kl

        x = self.gp2(x)
        x, kl = self.fc2(x)
        kl_sum += kl

        output = F.log_softmax(x, dim=1)
        return output, kl_sum
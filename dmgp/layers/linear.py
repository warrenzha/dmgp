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
# Linear Layers with reparameterization and flipout to perform
# variational inference in Bayesian neural networks. Reparameterization layers
# enables Monte Carlo approximation of the distribution over 'kernel' and 'bias'.
#
# Kullback-Leibler divergence between the surrogate posterior and prior is computed
# and returned along with the tensors of outputs after linear opertaion, which is
# required to compute Evidence Lower Bound (ELBO).
#
# @authors: Wenyuan Zhao. Some code snippets borrowed from: Intel Labs Bayeisan-Torch.
#
# ===============================================================================================


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import Parameter
from torch.distributions.normal import Normal
from torch.distributions.uniform import Uniform
from .base_variational_layer import _BaseVariationalLayer
from torch.quantization.observer import HistogramObserver, PerChannelMinMaxObserver, MinMaxObserver
from torch.quantization.qconfig import QConfig


class LinearReparameterization(_BaseVariationalLayer):
    r"""
    Implements Linear layer with reparameterization trick. Inherits from dmgp.layers._BaseVariationalLayer

    :param in_features: Size of each input sample.
    :type in_features: int
    :param out_features: Size of each output sample.
    :type out_features: int
    :param prior_mean: Mean of the prior arbitrary distribution to be used on the complexity cost. (Default: `0`.)
    :type prior_mean: float, optional
    :param prior_variance: Variance of the prior arbitrary distribution to be used on the complexity cost. (Default: `1.0`.)
    :type prior_variance: float, optional
    :param posterior_mu_init: Initialized trainable mu parameter representing mean of the approximate posterior. (Default: `0`.)
    :type posterior_mu_init: float, optional
    :param posterior_rho_init: Initialized trainable rho parameter representing the sigma of the approximate posterior through softplus function. (Default: `-3.0`.)
    :type posterior_rho_init: float, optional
    :param bias: If set to False, the layer will not learn an additive bias. (Default: `True`.)
    :type bias: bool, optional
    """

    def __init__(self,
                 in_features,
                 out_features,
                 prior_mean=0,
                 prior_variance=1,
                 posterior_mu_init=0,
                 posterior_rho_init=-3.0,
                 bias=True):
        super(LinearReparameterization, self).__init__()

        self.in_features = in_features
        self.out_features = out_features
        self.prior_mean = prior_mean
        self.prior_variance = prior_variance
        self.posterior_mu_init = posterior_mu_init,  # mean of weight
        self.posterior_rho_init = posterior_rho_init,  # variance of weight --> sigma = log (1 + exp(rho))
        self.bias = bias

        self.mu_weight = Parameter(torch.Tensor(out_features, in_features))
        self.rho_weight = Parameter(torch.Tensor(out_features, in_features))
        self.register_buffer('eps_weight',
                             torch.Tensor(out_features, in_features),
                             persistent=False)
        self.register_buffer('prior_weight_mu',
                             torch.Tensor(out_features, in_features),
                             persistent=False)
        self.register_buffer('prior_weight_sigma',
                             torch.Tensor(out_features, in_features),
                             persistent=False)
        if bias:
            self.mu_bias = Parameter(torch.Tensor(out_features))
            self.rho_bias = Parameter(torch.Tensor(out_features))
            self.register_buffer(
                'eps_bias',
                torch.Tensor(out_features),
                persistent=False)
            self.register_buffer(
                'prior_bias_mu',
                torch.Tensor(out_features),
                persistent=False)
            self.register_buffer('prior_bias_sigma',
                                 torch.Tensor(out_features),
                                 persistent=False)
        else:
            self.register_buffer('prior_bias_mu', None, persistent=False)
            self.register_buffer('prior_bias_sigma', None, persistent=False)
            self.register_parameter('mu_bias', None)
            self.register_parameter('rho_bias', None)
            self.register_buffer('eps_bias', None, persistent=False)

        self.init_parameters()
        self.quant_prepare = False

    def prepare(self):
        self.qint_quant = nn.ModuleList([torch.quantization.QuantStub(
            QConfig(weight=MinMaxObserver.with_args(dtype=torch.qint8, qscheme=torch.per_tensor_symmetric),
                    activation=MinMaxObserver.with_args(dtype=torch.qint8, qscheme=torch.per_tensor_symmetric))) for _
            in range(5)])
        self.quint_quant = nn.ModuleList([torch.quantization.QuantStub(
            QConfig(weight=MinMaxObserver.with_args(dtype=torch.quint8),
                    activation=MinMaxObserver.with_args(dtype=torch.quint8))) for _ in range(2)])
        self.dequant = torch.quantization.DeQuantStub()
        self.quant_prepare = True

    def init_parameters(self):
        self.prior_weight_mu.fill_(self.prior_mean)
        self.prior_weight_sigma.fill_(self.prior_variance)

        self.mu_weight.data.normal_(mean=self.posterior_mu_init[0], std=0.1)
        self.rho_weight.data.normal_(mean=self.posterior_rho_init[0], std=0.1)
        if self.mu_bias is not None:
            self.prior_bias_mu.fill_(self.prior_mean)
            self.prior_bias_sigma.fill_(self.prior_variance)
            self.mu_bias.data.normal_(mean=self.posterior_mu_init[0], std=0.1)
            self.rho_bias.data.normal_(mean=self.posterior_rho_init[0], std=0.1)

    def kl_loss(self):
        sigma_weight = torch.log1p(torch.exp(self.rho_weight))
        kl = self.kl_div(
            self.mu_weight,
            sigma_weight,
            self.prior_weight_mu,
            self.prior_weight_sigma)
        if self.mu_bias is not None:
            sigma_bias = torch.log1p(torch.exp(self.rho_bias))
            kl += self.kl_div(self.mu_bias, sigma_bias,
                              self.prior_bias_mu, self.prior_bias_sigma)
        return kl

    def forward(self, x, return_kl=True):
        r"""
        Forward the bayesian Linear layer.

        :param x: Training data of shape :math:`(n,d)`.
        :type x: torch.Tensor.float
        :param return_kl: Return KL-divergence. Default: `True`.
        :type return_kl: bool, optional

        :return: The output and KL-divergence.
        """

        if self.dnn_to_bnn_flag:
            return_kl = False
        sigma_weight = torch.log1p(torch.exp(self.rho_weight))
        eps_weight = self.eps_weight.data.normal_()
        tmp_result = sigma_weight * eps_weight
        weight = self.mu_weight + tmp_result

        if return_kl:
            kl_weight = self.kl_div(self.mu_weight, sigma_weight,
                                    self.prior_weight_mu, self.prior_weight_sigma)
        bias = None

        if self.mu_bias is not None:
            sigma_bias = torch.log1p(torch.exp(self.rho_bias))
            bias = self.mu_bias + (sigma_bias * self.eps_bias.data.normal_())
            if return_kl:
                kl_bias = self.kl_div(self.mu_bias, sigma_bias, self.prior_bias_mu,
                                      self.prior_bias_sigma)

        out = F.linear(x, weight, bias)

        if self.quant_prepare:
            # quint8 quantstrat
            x = self.quint_quant[0](x)  # input
            out = self.quint_quant[1](out)  # output

            # qint8 quantstrat
            sigma_weight = self.qint_quant[0](sigma_weight)  # weight
            mu_weight = self.qint_quant[1](self.mu_weight)  # weight
            eps_weight = self.qint_quant[2](eps_weight)  # random variable
            tmp_result = self.qint_quant[3](tmp_result)  # multiply activation
            weight = self.qint_quant[4](weight)  # add activation

        if return_kl:
            if self.mu_bias is not None:
                kl = kl_weight + kl_bias
            else:
                kl = kl_weight

            return out, kl

        return out


class LinearFlipout(_BaseVariationalLayer):
    r"""
    Implements Linear layer with Flipout reparameterization trick. Ref: https://arxiv.org/abs/1803.04386. Inherits from dmgp.layers._BaseVariationalLayer.

    :param in_features: Size of each input sample.
    :type in_features: int
    :param out_features: Size of each output sample.
    :type out_features: int
    :param prior_mean: Mean of the prior arbitrary distribution to be used on the complexity cost. (Default: `0`.)
    :type prior_mean: float, optional
    :param prior_variance: Variance of the prior arbitrary distribution to be used on the complexity cost. (Default: `1.0`.)
    :type prior_variance: float, optional
    :param posterior_mu_init: Initialized trainable mu parameter representing mean of the approximate posterior. (Default: `0`.)
    :type posterior_mu_init: float, optional
    :param posterior_rho_init: Initialized trainable rho parameter representing the sigma of the approximate posterior through softplus function. (Default: `-3.0`.)
    :type posterior_rho_init: float, optional
    :param bias: If set to False, the layer will not learn an additive bias. (Default: `True`.)
    :type bias: bool, optional
    """

    def __init__(self,
                 in_features,
                 out_features,
                 prior_mean=0,
                 prior_variance=1,
                 posterior_mu_init=0,
                 posterior_rho_init=-3.0,
                 bias=True):
        super(LinearFlipout, self).__init__()

        self.in_features = in_features
        self.out_features = out_features

        self.prior_mean = prior_mean
        self.prior_variance = prior_variance
        self.posterior_mu_init = posterior_mu_init
        self.posterior_rho_init = posterior_rho_init

        self.mu_weight = nn.Parameter(torch.Tensor(out_features, in_features))
        self.rho_weight = nn.Parameter(torch.Tensor(out_features, in_features))
        self.register_buffer('eps_weight',
                             torch.Tensor(out_features, in_features),
                             persistent=False)
        self.register_buffer('prior_weight_mu',
                             torch.Tensor(out_features, in_features),
                             persistent=False)
        self.register_buffer('prior_weight_sigma',
                             torch.Tensor(out_features, in_features),
                             persistent=False)

        if bias:
            self.mu_bias = nn.Parameter(torch.Tensor(out_features))
            self.rho_bias = nn.Parameter(torch.Tensor(out_features))
            self.register_buffer('prior_bias_mu', torch.Tensor(out_features), persistent=False)
            self.register_buffer('prior_bias_sigma',
                                 torch.Tensor(out_features),
                                 persistent=False)
            self.register_buffer('eps_bias', torch.Tensor(out_features), persistent=False)

        else:
            self.register_buffer('prior_bias_mu', None, persistent=False)
            self.register_buffer('prior_bias_sigma', None, persistent=False)
            self.register_parameter('mu_bias', None)
            self.register_parameter('rho_bias', None)
            self.register_buffer('eps_bias', None, persistent=False)

        self.init_parameters()
        self.quant_prepare = False

    def prepare(self):
        self.qint_quant = nn.ModuleList([torch.quantization.QuantStub(
            QConfig(weight=MinMaxObserver.with_args(dtype=torch.qint8, qscheme=torch.per_tensor_symmetric),
                    activation=MinMaxObserver.with_args(dtype=torch.qint8, qscheme=torch.per_tensor_symmetric))) for _
            in range(4)])
        self.quint_quant = nn.ModuleList([torch.quantization.QuantStub(
            QConfig(weight=MinMaxObserver.with_args(dtype=torch.quint8),
                    activation=MinMaxObserver.with_args(dtype=torch.quint8))) for _ in range(8)])
        self.dequant = torch.quantization.DeQuantStub()
        self.quant_prepare = True

    def init_parameters(self):
        # init prior mu
        self.prior_weight_mu.fill_(self.prior_mean)
        self.prior_weight_sigma.fill_(self.prior_variance)

        # init weight and base perturbation weights
        self.mu_weight.data.normal_(mean=self.posterior_mu_init, std=0.1)
        self.rho_weight.data.normal_(mean=self.posterior_rho_init, std=0.1)

        if self.mu_bias is not None:
            self.prior_bias_mu.fill_(self.prior_mean)
            self.prior_bias_sigma.fill_(self.prior_variance)
            self.mu_bias.data.normal_(mean=self.posterior_mu_init, std=0.1)
            self.rho_bias.data.normal_(mean=self.posterior_rho_init, std=0.1)

    def kl_loss(self):
        sigma_weight = torch.log1p(torch.exp(self.rho_weight))
        kl = self.kl_div(self.mu_weight, sigma_weight, self.prior_weight_mu, self.prior_weight_sigma)
        if self.mu_bias is not None:
            sigma_bias = torch.log1p(torch.exp(self.rho_bias))
            kl += self.kl_div(self.mu_bias, sigma_bias, self.prior_bias_mu, self.prior_bias_sigma)
        return kl

    def forward(self, x, return_kl=True):
        r"""
        Forward the bayesian Linear layer.

        :param x: Training data of shape :math:`(n,d)`.
        :type x: torch.Tensor.float
        :param return_kl: Return KL-divergence. Default: `True`.
        :type return_kl: bool, optional

        :return: The output and KL-divergence.
        """

        if self.dnn_to_bnn_flag:
            return_kl = False
        # sampling delta_W
        sigma_weight = torch.log1p(torch.exp(self.rho_weight))
        eps_weight = self.eps_weight.data.normal_()
        delta_weight = sigma_weight * eps_weight
        # delta_weight = (sigma_weight * self.eps_weight.data.normal_())

        # get kl divergence
        if return_kl:
            kl = self.kl_div(self.mu_weight, sigma_weight, self.prior_weight_mu,
                             self.prior_weight_sigma)

        bias = None
        if self.mu_bias is not None:
            sigma_bias = torch.log1p(torch.exp(self.rho_bias))
            bias = (sigma_bias * self.eps_bias.data.normal_())
            if return_kl:
                kl = kl + self.kl_div(self.mu_bias, sigma_bias, self.prior_bias_mu,
                                      self.prior_bias_sigma)

        # linear outputs
        outputs = F.linear(x, self.mu_weight, self.mu_bias)
        sign_input = x.clone().uniform_(-1, 1).sign()
        sign_output = outputs.clone().uniform_(-1, 1).sign()
        x_tmp = x * sign_input
        perturbed_outputs_tmp = F.linear(x_tmp, delta_weight, bias)
        perturbed_outputs = perturbed_outputs_tmp * sign_output
        out = outputs + perturbed_outputs

        if self.quant_prepare:
            # quint8 quantstub
            x = self.quint_quant[0](x)  # input
            outputs = self.quint_quant[1](outputs)  # output
            sign_input = self.quint_quant[2](sign_input)
            sign_output = self.quint_quant[3](sign_output)
            x_tmp = self.quint_quant[4](x_tmp)
            perturbed_outputs_tmp = self.quint_quant[5](perturbed_outputs_tmp)  # output
            perturbed_outputs = self.quint_quant[6](perturbed_outputs)  # output
            out = self.quint_quant[7](out)  # output

            # qint8 quantstub
            sigma_weight = self.qint_quant[0](sigma_weight)  # weight
            mu_weight = self.qint_quant[1](self.mu_weight)  # weight
            eps_weight = self.qint_quant[2](eps_weight)  # random variable
            delta_weight = self.qint_quant[3](delta_weight)  # multiply activation

        # returning outputs + perturbations
        if return_kl:
            return out, kl
        return out
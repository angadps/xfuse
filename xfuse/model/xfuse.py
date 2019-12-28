from typing import Dict, List

import pyro as p
from actnorm import ActNorm2d
from pyro.distributions import Normal  # pylint: disable=no-name-in-module
from pyro.poutine.messenger import Messenger

import torch

from .experiment import Experiment
from ..logging import INFO, log
from ..utility import find_device
from ..utility.modules import get_module


class ModelWrapper(Messenger):
    r""":class:`Messenger` wrapping the model to add context information."""

    def _process_message(self, msg):
        msg["is_guide"] = False


class GuideWrapper(Messenger):
    r""":class:`Messenger` wrapping the guide to add context information."""

    def _process_message(self, msg):
        msg["is_guide"] = True


class XFuse(torch.nn.Module):
    r"""XFuse"""

    def __init__(self, experiments: List[Experiment]):
        super().__init__()
        self.__experiment_store: Dict[str, Experiment] = {}
        for experiment in experiments:
            self.register_experiment(experiment)

    @property
    def experiments(self):
        r"""Returns the registered experiments"""
        return self.__experiment_store.copy()

    def get_experiment(self, experiment_type: str) -> Experiment:
        r"""Get registered :class:`Experiment` by tag"""

        try:
            return self.__experiment_store[experiment_type]
        except KeyError:
            raise RuntimeError(f"unknown experiment type: {experiment_type}")

    def register_experiment(self, experiment: Experiment) -> None:
        r"""Registered :class:`Experiment`"""

        if experiment.tag in self.__experiment_store:
            raise RuntimeError(
                f'model for data type "{experiment.tag}" already registered'
            )
        log(
            INFO,
            'registering experiment: %s (data type: "%s")',
            type(experiment).__name__,
            experiment.tag,
        )
        self.add_module(experiment.tag, experiment)
        self.__experiment_store[experiment.tag] = experiment

    def forward(self, *input):
        # pylint: disable=redefined-builtin
        return self.model(*input)

    @ModelWrapper()
    def model(self, xs):
        r"""Runs XFuse on the given data"""

        def _go(experiment, x):
            with p.poutine.scale(scale=experiment.n / len(x)):
                zs = [
                    p.sample(
                        f"z-{experiment.tag}-{i}",
                        (
                            # pylint: disable=not-callable
                            Normal(
                                torch.tensor(0.0, device=find_device(x)), 1.0
                            )
                            .expand([1, 1, 1, 1])
                            .to_event(3)
                        ),
                    )
                    for i in range(experiment.num_z)
                ]
            return experiment.model(x, zs)

        return {e: _go(self.get_experiment(e), x) for e, x in xs.items()}

    @GuideWrapper()
    def guide(self, xs):
        r"""
        Runs the :class:`pyro.infer.SVI` `guide` for XFuse on the given data
        """

        def _go(experiment, x):
            def _sample(name, y):
                z_mu = get_module(
                    f"{name}-mu",
                    lambda: torch.nn.Sequential(
                        torch.nn.Conv2d(y.shape[1], y.shape[1], 1),
                        ActNorm2d(y.shape[1]),
                        torch.nn.LeakyReLU(0.2, inplace=True),
                        torch.nn.Conv2d(y.shape[1], y.shape[1], 1),
                    ),
                ).to(y)
                z_sd = get_module(
                    f"{name}-sd",
                    lambda: torch.nn.Sequential(
                        torch.nn.Conv2d(y.shape[1], y.shape[1], 1),
                        ActNorm2d(y.shape[1]),
                        torch.nn.LeakyReLU(0.2, inplace=True),
                        torch.nn.Conv2d(y.shape[1], y.shape[1], 1),
                        torch.nn.Softplus(),
                    ),
                ).to(y)
                with p.poutine.scale(scale=experiment.n / len(x)):
                    return p.sample(
                        name, Normal(z_mu(y), 1e-8 + z_sd(y)).to_event(3)
                    )

            ys = experiment.guide(x)
            zs = [
                _sample(f"z-{experiment.tag}-{i}", y) for i, y in enumerate(ys)
            ]
            return zs

        return {e: _go(self.get_experiment(e), x) for e, x in xs.items()}
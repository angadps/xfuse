# pylint: disable=missing-docstring, invalid-name, too-many-instance-attributes

from contextlib import ExitStack

from datetime import datetime as dt

from functools import wraps

from inspect import getargs

import itertools as it

import os

import sys

import click

import pandas as pd

from pyvips import Image

import pyro as p
import pyro.optim

import torch as t
from torch.utils.tensorboard.writer import SummaryWriter

from . import __version__
from .data import Dataset
from .data.slide import RandomSlide
from .data.utility import make_dataloader, spot_size
from .handlers import Checkpointer, stats
from .logging import (
    DEBUG,
    INFO,
    WARNING,
    log,
)
from .model import XFuse
from .model.experiment.st import (
    ST,
    FactorDefault,
    FactorPurger,
    purge_factors,
)
from .session import (
    Session,
    Unset,
    get_default_device,
    get_global_step,
    get_model,
    get_save_path,
)
from .train import train as run_training
from .utility import (
    compose,
    design_matrix_from,
    read_data,
    set_rng_seed,
    with_,
)
from .utility.file import unique_prefix
from .utility.session import load_session, save_session


_DEFAULT_SESSION = Session()


def _init(f):
    @wraps(f)
    def _wrapped(*args, **kwargs):
        log(INFO, 'this is %s %s', __package__, __version__)
        log(DEBUG, 'invoked by %s', ' '.join(sys.argv))
        return f(*args, **kwargs)
    return _wrapped


@click.group()
@click.option('--save-path', type=str)
@click.option('--session', type=click.Path(resolve_path=True))
@click.option('-v', '--verbose', is_flag=True)
@click.version_option()
def cli(save_path, session, verbose):
    if session is not None:
        for k, v in load_session(session):
            setattr(_DEFAULT_SESSION, k, v)

    if save_path is not None:
        _DEFAULT_SESSION.save_path = save_path
    elif isinstance(_DEFAULT_SESSION.save_path, Unset):
        _DEFAULT_SESSION.save_path = f'{__package__}-{dt.now().isoformat()}'

    if verbose:
        _DEFAULT_SESSION.log_level = -999

    _DEFAULT_SESSION.log_file = unique_prefix(os.path.join(
        _DEFAULT_SESSION.save_path, 'log'))


@click.command()
@click.argument('design-file', type=click.File('rb'))
@click.option('--batch-size', type=int, default=8)
@click.option('--checkpoint-interval', type=int)
@click.option('--epochs', type=int)
@click.option('--image', 'image_interval', type=int, default=1000)
@click.option('--latent-size', type=int, default=32)
@click.option('--learning-rate', type=float, default=2e-4)
@click.option('--network-depth', type=int, default=4)
@click.option('--network-width', type=int, default=8)
@click.option('--patch-size', type=int, default=512)
@click.option('--seed', type=int)
@click.option('--workers', type=int, default=0)
@with_(_DEFAULT_SESSION)
@_init
def train(
        design_file,
        batch_size,
        checkpoint_interval,
        epochs,
        latent_size,
        learning_rate,
        network_depth,
        network_width,
        patch_size,
        seed,
        workers,
        **kwargs,
):
    if seed is not None:
        set_rng_seed(seed)

        if workers is None:
            log(WARNING,
                'setting workers to 0 to avoid race conditions '
                '(set --workers explicitly to override)')
            workers = 0

    design = pd.read_csv(design_file)
    design_dir = os.path.dirname(design_file.name)

    def _path(p):
        return (
            p
            if os.path.isabs(p) else
            os.path.join(design_dir, p)
        )

    count_data = read_data(map(_path, design.data))

    design_matrix = design_matrix_from(design[[
        x for x in design.columns
        if x not in [
                'name',
                'image',
                'labels',
                'data',
        ]
    ]])

    dataset = Dataset(
        [
            RandomSlide(
                data=counts,
                image=Image.new_from_file(_path(image)),
                label=Image.new_from_file(_path(labels)),
                patch_size=patch_size,
            )
            for image, labels, counts in zip(
                design.image,
                design.labels,
                (count_data.loc[x] for x in count_data.index.levels[0]),
            )
        ],
        design_matrix,
    )

    dataloader = make_dataloader(
        dataset,
        batch_size=batch_size,
        num_workers=workers,
        shuffle=True,
    )

    factor_baseline = t.as_tensor(count_data.mean(0).values).log()

    if get_model() is None:
        st_experiment = ST(
            n=len(dataset),
            depth=network_depth,
            nc=network_width,
            default_scale=count_data.mean().mean() / spot_size(dataset),
            factors=[FactorDefault(0., factor_baseline)],
        )
        xfuse = XFuse(
            experiments=[st_experiment],
            latent_size=latent_size,
        ).to(get_default_device())
        default_session = Session(
            model=xfuse,
            optimizer=p.optim.Adam({'lr': learning_rate}),
        )
    else:
        default_session = Session()

    def _panic(session, err_type, err, tb):
        with Session(panic=Unset):
            save_session(f'exception')

    with default_session, Session(panic=_panic):
        def _every(n):
            def _predicate(**msg):
                if int(get_global_step()) % n == 0:
                    return True
                return False
            return _predicate

        writer = SummaryWriter(os.path.join(get_save_path(), 'stats'))

        stats_handlers = [
            stats.ELBO(writer, _every(1)),
            stats.FactorActivationHistogram(writer, _every(10)),
            stats.FactorActivationMaps(writer, _every(100)),
            stats.FactorActivationMean(writer, _every(1)),
            stats.FactorActivationSummary(writer, _every(100)),
            stats.Image(writer, _every(100)),
            stats.Latent(writer, _every(100)),
            stats.LogLikelihood(writer, _every(1)),
            stats.RMSE(writer, _every(1)),
            stats.Scale(writer, _every(100)),
        ]

        contexts = [
            Checkpointer(frequency=checkpoint_interval),
            FactorPurger(
                dataloader,
                frequency=100,
                baseline=factor_baseline,
            ),
        ]

        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            for stats_handler in stats_handlers:
                stack.enter_context(stats_handler)

            run_training(dataloader, epochs)

        purge_factors(get_model(), dataloader, num_samples=10)

        with Session(panic=Unset):
            save_session(f'final')


cli.add_command(train)


@click.group(chain=True)
@click.option(
    '--state-file',
    '--state',
    '--restore',
    type=click.File('rb'),
    required=True,
)
@click.option(
    '--design-file',
    '--design',
    type=click.File('rb'),
)
@click.option(
    '-o', '--save-path',
    type=click.Path(resolve_path=True),
    default=f'{__package__}-{dt.now().isoformat()}',
)
def analyze(**_):
    pass


@analyze.resultcallback()
def _run_analysis(analyses, design_file, state_file, output):
    state = load_state(state_file.name)
    to_device(state, DEVICE)

    t.no_grad()
    state.histonet.eval()
    state.std.eval()

    design = pd.read_csv(design_file)
    design_dir = os.path.dirname(design_file.name)

    def _path(p):
        return (
            p
            if os.path.isabs(p) else
            os.path.join(design_dir, p)
        )

    data = read_data(map(_path, design.data), genes=state.std.genes)
    design_matrix = design_matrix_from(design, state.std._covariates)
    samples = [
        Sample(
            name=name,
            image=image,
            label=label,
            data=data,
            effects=effects,
        )
        for name, image, label, data, effects in it.zip_longest(
                (
                    design.name
                    if 'name' in design.columns else
                    [f'sample_{i + 1}' for i in range(design.shape[0])]
                ),
                map(compose(Image.new_from_file, _path), design.image),
                (
                    map(
                        compose(Image.new_from_file, _path),
                        design.labels,
                    )
                    if 'labels' in design.columns else
                    []
                ),
                [data.xs(a, level=0) for a in data.index.levels[0]],
                design_matrix.values.transpose(),
        )
    ]

    for name, analysis in analyses:
        log(INFO, 'performing analysis: %s', name)
        if getargs(analysis.__code__).args == ['state', 'samples', 'output']:
            analysis(state=state, samples=samples, output=output)
        elif getargs(analysis.__code__).args == ['state', 'sample', 'output']:
            for sample in samples:
                log(INFO, 'processing %s', sample.name)
                output_prefix = os.path.join(output, sample.name)
                os.makedirs(output_prefix, exist_ok=True)
                analysis(
                    state=state,
                    sample=sample,
                    output=output_prefix,
                )
        else:
            raise RuntimeError(
                f'the signature of analysis "{name}" is not supported')


cli.add_command(analyze)


@click.command()
@click.argument('gene-list', nargs=-1)
def genes(gene_list):
    def _analysis(state, sample, output):
        analyze_genes(
            state.histonet,
            state.std,
            sample,
            gene_list,
            output_prefix=output,
            device=DEVICE,
        )
    return 'gene list', _analysis


analyze.add_command(genes)


@click.command()
@click.argument('gene-list', nargs=-1)
@click.option('--factor', type=int, multiple=True)
@click.option('--truncate', type=int, default=25)
@click.option('--regex/--no-regex', default=True)
def gene_profiles(gene_list, factor, truncate, regex):
    def _analysis(state, samples, output):
        analyze_gene_profiles(
            std=state.std,
            genes=list(gene_list),
            factors=factor if len(factor) > 0 else None,
            truncate=truncate,
            regex=regex,
            output_prefix=output,
        )
    return 'gene profiles', _analysis


analyze.add_command(gene_profiles)


@click.command()
def default():
    def _analysis(state, sample, output):
        default_analysis(
            state.histonet,
            state.std,
            sample,
            output_prefix=output,
            device=DEVICE,
        )
    return 'default', _analysis


analyze.add_command(default)


@click.command()
@click.argument(
    'regions-file',
    metavar='regions',
    type=click.File('rb'),
)
def impute(regions_file):
    regions = pd.read_csv(regions_file)

    regions_dir = os.path.dirname(regions_file.name)

    def _path(p):
        return (
            p
            if os.path.isabs(p) else
            os.path.join(regions_dir, p)
        )

    if 'regions' not in regions.columns:
        raise ValueError('regions file must contain a "regions" column')

    def _analysis(state, samples, output, **_):
        nonlocal regions

        if 'name' in regions.columns:
            samples_dict = {s.name: s for s in samples}

            def _sample(n):
                if n not in samples_dict:
                    raise ValueError(f'name "{n}" is not in the design file')
                return samples_dict[n]

            samples = [*map(_sample, regions.name)]
        else:
            if len(regions) != len(samples):
                raise ValueError(
                    'if the regions file does not contain a "name" column, '
                    'it must have the same length as the design file.'
                )

        regions = [
            Image.new_from_file(os.path.join(regions_dir, r))
            for r in regions.regions
        ]

        for sample, region in zip(samples, regions):
            means, samples, index = impute_counts(
                state.histonet,
                state.std,
                sample,
                region,
                device=DEVICE,
            )
            os.makedirs(os.path.join(output, sample.name))
            (
                pd.DataFrame(
                    means.mean(0).numpy(),
                    index=pd.Index(index, name='n'),
                    columns=state.std.genes,
                )
                .to_csv(os.path.join(output, sample.name, 'imputed.csv.gz'))
            )
            (
                pd.concat(
                    [
                        pd.DataFrame(
                            s.numpy().astype(int),
                            index=pd.Index(index, name='n'),
                            columns=state.std.genes,
                        )
                        for s in samples
                    ],
                    keys=list(range(len(samples))),
                    names=['sample'],
                )
                .to_csv(os.path.join(output, sample.name, 'samples.csv.gz'))
            )
    return 'imputation', _analysis


analyze.add_command(impute)


@click.command()
@click.argument(
    'regions-file',
    metavar='regions',
    type=click.File('rb'),
)
@click.option('--normalize/--no-normalize', default=True)
@click.option('--trials', type=int, default=100)
def dge(regions_file, normalize, trials):
    regions = pd.read_csv(regions_file)

    regions_dir = os.path.dirname(regions_file.name)

    def _path(p):
        return (
            p
            if os.path.isabs(p) else
            os.path.join(regions_dir, p)
        )

    if 'regions' not in regions.columns:
        raise ValueError('regions file must contain a "regions" column')

    def _analysis(state, samples, output, **_):
        nonlocal regions

        if 'name' in regions.columns:
            samples_dict = {s.name: s for s in samples}

            def _sample(n):
                if n not in samples_dict:
                    raise ValueError(f'name "{n}" is not in the design file')
                return samples_dict[n]

            samples = [*map(_sample, regions.name)]
        else:
            if len(regions) != len(samples):
                raise ValueError(
                    'if the regions file does not contain a "name" column, '
                    'it must have the same length as the design file.'
                )

        regions = [
            Image.new_from_file(os.path.join(regions_dir, r))
            for r in regions.regions
        ]

        dge_analysis(
            state.histonet,
            state.std,
            samples=samples,
            regions=regions,
            output=output,
            normalize=normalize,
            trials=trials,
            device=DEVICE,
        )

    return 'differential gene expression', _analysis


analyze.add_command(dge)


if __name__ == '__main__':
    cli()

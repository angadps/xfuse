# pylint: disable=missing-docstring, invalid-name, too-many-instance-attributes

import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime as dt
from functools import wraps

import click
import h5py
import ipdb
import pandas as pd
import tomlkit

from imageio import imread

from . import __version__, convert
from ._config import (  # type: ignore
    construct_default_config_toml,
    merge_config,
)
from .logging import DEBUG, log
from .model.experiment.st import STRATEGIES as expansion_strategies
from .run import run as _run
from .session import Session
from .utility import design_matrix_from
from .utility.file import first_unique_filename
from .utility.session import load_session

_DEFAULT_SESSION = Session()


def _init(f):
    @wraps(f)
    def _wrapped(*args, **kwargs):
        log(DEBUG, "this is %s %s", __package__, __version__)
        log(DEBUG, "invoked by %s", " ".join(sys.argv))
        return f(*args, **kwargs)

    return _wrapped


@click.group()
@click.option("--debug", is_flag=True)
@click.version_option()
@_init
def cli(debug):
    if debug:

        def _panic(_s, _err_type, _err, tb):
            ipdb.post_mortem(tb)

        _DEFAULT_SESSION.panic = _panic
        _DEFAULT_SESSION.log_level = -999


@click.group("convert")
def _convert():
    r"""Converts data of various formats to the format used by xfuse."""


cli.add_command(_convert)


@click.command()
@click.option("--image", type=click.File("rb"), required=True)
@click.option("--bc-matrix", type=click.File("rb"), required=True)
@click.option("--tissue-positions", type=click.File("rb"), required=True)
@click.option("--scale-factors", type=click.File("rb"), required=True)
@click.option("--output-file", type=click.Path(), default="slide.h5")
def visium(image, bc_matrix, tissue_positions, scale_factors, output_file):
    r"""Converts 10X Visium data to the format used by xfuse."""
    scale_factors = json.load(scale_factors)
    spot_radius = scale_factors["spot_diameter_fullres"] / 2
    spot_radius = spot_radius * scale_factors["tissue_hires_scalef"]
    tissue_positions = pd.read_csv(tissue_positions, index_col=0, header=None)
    tissue_positions = tissue_positions[[4, 5]]
    tissue_positions = tissue_positions.rename(columns={4: "y", 5: "x"})
    tissue_positions = tissue_positions * scale_factors["tissue_hires_scalef"]
    image = imread(image)
    with h5py.File(bc_matrix, "r") as data:
        convert.visium.run(
            image, data, tissue_positions, spot_radius, output_file
        )


_convert.add_command(visium)


@click.command()
@click.argument("target", type=click.Path(), default=f"{__package__}.toml")
@click.argument(
    "slides", type=click.Path(exists=True, dir_okay=False), nargs=-1,
)
def init(target, slides):
    r"""Creates a template for the project configuration file."""
    config = construct_default_config_toml()
    if len(slides) > 0:
        config["slides"] = {slide: {} for slide in slides}
    with open(target, "w") as fp:
        fp.write(config.as_string())


cli.add_command(init)


@click.command()
@click.argument("project-file", type=click.File("rb"))
@click.option(
    "--save-path",
    type=click.Path(),
    default=f"hssl-{dt.now().isoformat()}",
    help="The output path",
    show_default=True,
)
@click.option("--session", type=click.File("rb"))
def run(project_file, save_path, session):
    r"""
    Runs xfuse based on a project configuration file.
    The configuration file can be created manually or using the `init`
    subcommand.
    """

    config = dict(tomlkit.loads(project_file.read().decode()))
    config = merge_config(config)

    if os.path.dirname(project_file.name) != save_path:
        os.makedirs(save_path, exist_ok=True)
        with open(
            first_unique_filename(os.path.join(save_path, "config.toml")), "w"
        ) as f:
            f.write(tomlkit.dumps(config))

    session_stack = [_DEFAULT_SESSION]
    if session is not None:
        session_stack.append(load_session(session))
    session_stack.append(
        Session(
            save_path=save_path,
            log_file=first_unique_filename(os.path.join(save_path, "log")),
        )
    )

    design = design_matrix_from(config["slides"])
    design.columns = [
        x
        if os.path.isabs(x)
        else os.path.join(os.path.dirname(project_file.name), x)
        for x in map(os.path.expanduser, design.columns)
    ]

    with ExitStack() as stack:
        for ctx in session_stack:
            stack.enter_context(ctx)
        _run(
            design,
            expansion_strategy=expansion_strategies[
                config["expansion-strategy"]["type"]
            ](
                **config["expansion-strategy"][
                    config["expansion-strategy"]["type"]
                ]
            ),
            network_depth=config["xfuse"]["network-depth"],
            network_width=config["xfuse"]["network-width"],
            patch_size=config["optimization"]["patch-size"],
            batch_size=config["optimization"]["batch-size"],
            epochs=config["optimization"]["epochs"],
            learning_rate=config["optimization"]["learning-rate"],
        )


cli.add_command(run)


if __name__ == "__main__":
    cli()  # pylint: disable=no-value-for-parameter

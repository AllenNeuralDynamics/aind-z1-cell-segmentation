"""
Large-scale prediction of gradients. We are
computing gradients in entire 2D planes which
are ZY, ZX and XY.
"""

import multiprocessing
import os
from time import time
from typing import Callable, List, Optional, Tuple

import numpy as np
import psutil
import utils
import zarr
from aind_large_scale_prediction.generator.dataset import create_data_loader
from aind_large_scale_prediction.generator.utils import recover_global_position
from cellpose.core import run_net, use_gpu
from cellpose.io import logger_setup
from cellpose.models import CellposeModel, assign_device, transforms

from .._shared import ArrayLike, PathLike


def run_2D_cellpose(
    net: CellposeModel,
    imgs: ArrayLike,
    img_axis: int,
    batch_size: Optional[int] = 8,
    rsz: Optional[float] = 1.0,
    anisotropy: Optional[float] = None,
    augment: Optional[bool] = False,
    tile: Optional[bool] = True,
    tile_overlap: Optional[float] = 0.1,
    bsize: Optional[int] = 224,
    progress=None,
) -> Tuple[ArrayLike]:
    """
    Runs cellpose on 2D images.

    Parameters
    ----------
    net: CellposeModel
        Initialized cellpose model to run
        inference on.

    imgs: ArrayLike
        Images to run inference on.

    img_axis: int
        Integer pointing to the image axis
        these 2D images belong to. Our images
        are in ZYX order.

    batch_size: Optional[int]
        Batch size. Default: 8

    rsz: Optional[float] = 1.0
        Rescaling factor in each dimension.
        Default: 1.0

    anisotropy: Optional[float] = None
        Anisotropy between orientations.
        Default: None

    augment: Optional[bool] = False
        tiles image with overlapping tiles and flips overlapped regions to augment.
        Default: False.

    tile: Optional[bool] = True
        tiles image to ensure GPU/CPU memory usage limited (recommended).
        Default: True.

    tile_overlap: Optional[float] = 0.1
        Fraction of overlap of tiles when computing flows.
        Default: 0.1.

    bsize: Optional[int] = 224
        block size for tiles, recommended to keep at 224, like in training.
        Default: 224.

    Returns
    -------
    Tuple[ArrayLike, ArrayLike]
        Predicted gradients and style.
    """
    sstr = ["XY", "ZX", "ZY"]
    if anisotropy is not None:
        rescaling = [[rsz, rsz], [rsz * anisotropy, rsz], [rsz * anisotropy, rsz]]
    else:
        rescaling = [rsz] * 3

    pm = [(0, 1, 2, 3), (1, 0, 2, 3), (2, 0, 1, 3)]
    ipm = [(3, 0, 1, 2), (3, 1, 0, 2), (3, 1, 2, 0)]
    xsl = imgs.copy().transpose(pm[img_axis])

    shape = xsl.shape
    xsl = transforms.resize_image(xsl, rsz=rescaling[img_axis])

    print(
        "running %s: %d planes of size (%d, %d)"
        % (sstr[img_axis], shape[0], shape[1], shape[2])
    )
    y, style = run_net(
        net,
        xsl,
        batch_size=batch_size,
        augment=augment,
        tile=tile,
        bsize=bsize,
        tile_overlap=tile_overlap,
    )

    y = transforms.resize_image(y, shape[1], shape[2])
    y = y.transpose(ipm[img_axis])

    if progress is not None:
        progress.setValue(25 + 15 * img_axis)

    return y, style


def run_cellpose_net(
    data: ArrayLike,
    model: CellposeModel,
    axis: int,
    normalize: Optional[bool] = True,
    diameter: Optional[int] = 15,
    anisotropy: Optional[float] = 1.0,
) -> ArrayLike:
    """
    Runs cellpose in stacks of 2D images.

    Parameters
    ----------
    data: ArrayLike
        Stack of 2D images to be processed.

    model: CellposeModel
        Initialized cellpose model.

    axis: int
        Image axis to be processed.

    normalize: Optional[bool]
        If we want to normalize the data
        using percentile normalization.
        Default: True

    diameter: Optional[int]
        Mean cell diameter
        Default: 15

    anisotropy: Optional[float]
        Anisotropy factor

    Returns
    -------
    ArrayLike
        Gradient prediction
    """
    data_converted = transforms.convert_image(
        data, None, channel_axis=None, z_axis=0, do_3D=True, nchan=model.nchan
    )

    if data_converted.ndim < 4:
        data_converted = data_converted[np.newaxis, ...]

    if diameter is not None and diameter > 0:
        rescale = model.diam_mean / diameter

    elif rescale is None:
        diameter = model.diam_labels
        rescale = model.diam_mean / diameter

    normalize_default = {
        "lowhigh": None,
        "percentile": None,
        "normalize": normalize,
        "norm3D": False,
        "sharpen_radius": 0,
        "smooth_radius": 0,
        "tile_norm_blocksize": 0,
        "tile_norm_smooth3D": 1,
        "invert": False,
    }

    x = np.asarray(data_converted)
    x = transforms.normalize_img(x, **normalize_default)

    y, style = run_2D_cellpose(
        model.net,
        x,
        p=axis,
        rsz=rescale,
        anisotropy=anisotropy,
        augment=False,
        tile=True,
        tile_overlap=0.1,
    )

    return y


def large_scale_cellpose_gradients_per_axis(
    dataset_path: PathLike,
    multiscale: str,
    output_gradients_path: PathLike,
    axis: int,
    prediction_chunksize: Tuple[int, ...],
    target_size_mb: int,
    n_workers: int,
    batch_size: int,
    super_chunksize: Optional[Tuple[int, ...]] = None,
    lazy_callback_fn: Optional[Callable[[ArrayLike], ArrayLike]] = None,
    normalize_image: Optional[bool] = True,
    model_name: Optional[str] = "cyto",
    cell_diameter: Optional[int] = 15,
):
    """
    Large-scale cellpose prediction of gradients.
    We estimate the gradients using entire 2D planes in
    XY, ZX and ZY directions. Cellpose is in nature a 2D
    network, therefore, there is no degradation in the
    prediction. We save these gradient estimation in
    each axis in a zarr dataset.

    Parameters
    ----------
    dataset_path: PathLike
        Path where the dataset in Zarr format is located.
        If the data is in the cloud, please provide the
        path to it. E.g., s3://bucket-name/path/image.zarr

    multiscale: str
        Dataset name insize the zarr dataset. If the zarr
        dataset is not organized in a folder structure,
        please use '.'

    output_gradients_path: PathLike
        Path where we want to output the estimated gradients
        in each plane.

    axis: int
        Axis that we are currently using for the estimation.

    prediction_chunksize: Tuple[int, ...]
        Prediction chunksize.

    target_size_mb: int
        Parameter used to load a super chunk from the zarr dataset.
        This improves i/o operations and should be bigger than the
        prediction chunksize. Please, verify the amount of available
        shared memory in your system to set this parameter.

    n_workers: int
        Number of workers that will be pulling data from the
        super chunk.

    batch_size: int
        Number of prediction chunksize blocks that will be pulled
        per worker

    super_chunksize: Optional[Tuple[int, ...]]
        Super chunk size. Could be None if target_size_mb is provided.
        Default: None

    lazy_callback_fn: Optional[Callable[[ArrayLike], ArrayLike]] = None
        Lazy callback function that will be applied to each of the chunks
        before they are sent to the GPU for prediction. E.g., we might need
        to run deskewing before we run prediction.

    normalize_image: Optional[bool] = True
        If we want to normalize the data for cellpose.

    model_name: Optional[str] = "cyto"
        Model name to be used by cellpose

    cell_diameter: Optional[int] = 15
        Cell diameter for cellpose

    """
    results_folder = os.path.abspath("../results")

    co_cpus = int(utils.get_code_ocean_cpu_limit())

    if n_workers > co_cpus:
        raise ValueError(f"Provided workers {n_workers} > current workers {co_cpus}")

    logger = utils.create_logger(output_log_path=results_folder)
    logger.info(f"{20*'='} Z1 Large-Scale Cellpose Segmentation {20*'='}")

    utils.print_system_information(logger)

    logger.info(f"Processing dataset {dataset_path} with mulsticale {multiscale}")

    # Tracking compute resources
    # Subprocess to track used resources
    manager = multiprocessing.Manager()
    time_points = manager.list()
    cpu_percentages = manager.list()
    memory_usages = manager.list()

    profile_process = multiprocessing.Process(
        target=utils.profile_resources,
        args=(
            time_points,
            cpu_percentages,
            memory_usages,
            20,
        ),
    )
    profile_process.daemon = True
    profile_process.start()

    # Creating zarr data loader
    logger.info("Creating chunked data loader")
    shm_memory = psutil.virtual_memory()
    logger.info(f"Shared memory information: {shm_memory}")

    # The device we will use and pinning memory to speed things up
    # and preallocate space in GPU
    device = None

    pin_memory = True
    if device is not None:
        pin_memory = False
        multiprocessing.set_start_method("spawn", force=True)

    # Overlap between prediction chunks, this overlap happens in every axis
    overlap_prediction_chunksize = (
        0,
        0,
        0,
    )

    # Creating zarr data loader
    zarr_data_loader, zarr_dataset = create_data_loader(
        dataset_path=dataset_path,
        multiscale=multiscale,
        target_size_mb=target_size_mb,
        prediction_chunksize=prediction_chunksize,
        overlap_prediction_chunksize=overlap_prediction_chunksize,
        n_workers=n_workers,
        batch_size=batch_size,
        dtype=np.float32,  # Allowed data type to process with pytorch cuda
        super_chunksize=super_chunksize,
        lazy_callback_fn=None,  # partial_lazy_deskewing,
        logger=logger,
        device=device,
        pin_memory=pin_memory,
        override_suggested_cpus=False,
        drop_last=True,
        locked_array=False,
    )

    shape = None
    # Creating or reading dataset depending of which axis we're processing
    if axis == 0:
        logger.info(f"Creating zarr gradients in path: {output_gradients_path}")
        output_gradients = zarr.open(
            output_gradients_path,
            "w",
            shape=(
                3,
                3,
            )
            + zarr_dataset.lazy_data.shape,
            chunks=(
                1,
                3,
            )
            + tuple(prediction_chunksize),
            dtype=np.float32,
        )
        shape = zarr_dataset.lazy_data.shape

    else:
        # Reading back output gradients
        output_gradients = zarr.open(
            output_gradients_path,
            "a",
        )
        shape = output_gradients.shape

    logger.info(
        f"Gradients: {output_gradients} chunks: {output_gradients.chunks} - Current shape: {shape}"
    )

    # Setting up cellpose
    use_GPU = use_gpu()
    logger.info(f"GPU activated: {use_GPU}")
    logger_setup()

    # Getting current GPU device and inizialing cellpose network
    sdevice, gpu = assign_device(use_torch=use_GPU, gpu=use_GPU)
    model = CellposeModel(
        gpu=gpu, model_type=model_name, diam_mean=cell_diameter, device=sdevice
    )

    # Estimating total batches
    total_batches = np.prod(zarr_dataset.lazy_data.shape) / (
        np.prod(zarr_dataset.prediction_chunksize) * batch_size
    )
    samples_per_iter = n_workers * batch_size
    logger.info(
        f"Number of batches: {total_batches} - Samples per iteration: {samples_per_iter}"
    )

    logger.info(
        f"{20*'='} Starting estimation of cellpose combined gradients - Axis {axis} {20*'='}"
    )
    start_time = time()

    # Processing entire dataset
    for i, sample in enumerate(zarr_data_loader):
        data = sample.batch_tensor.numpy()[0, ...]

        if data.shape != prediction_chunksize:
            logger.info(
                f"Non-uniform block of data... {data.shape} - {prediction_chunksize}"
            )
            continue

        # Recover global position of internal chunk
        (
            global_coord_pos,
            global_coord_positions_start,
            global_coord_positions_end,
        ) = recover_global_position(
            super_chunk_slice=sample.batch_super_chunk[0],
            internal_slices=sample.batch_internal_slice,
        )

        global_coord_pos = (slice(axis, axis + 1), slice(0, 3)) + global_coord_pos

        # Estimating plane gradient
        y = run_cellpose_net(
            data=data,
            model=model,
            axis=axis,
            compute_masks=False,
            batch_size=8,
            normalize=normalize_image,
            diameter=15,
            rsz=1.0,
            anisotropy=1.0,
        )

        global_coord_pos = list(global_coord_pos)

        if shape[-1] < global_coord_pos[-1].stop:
            global_coord_pos[-1] = slice(global_coord_pos[-1].start, shape[-1])

        if shape[-2] < global_coord_pos[-2].stop:
            global_coord_pos[-2] = slice(global_coord_pos[-2].start, shape[-2])

        if shape[-3] < global_coord_pos[-3].stop:
            global_coord_pos[-3] = slice(global_coord_pos[-3].start, shape[-3])

        global_coord_pos = tuple(global_coord_pos)
        logger.info(f"Writing to: {global_coord_pos}")

        output_gradients[global_coord_pos] = np.expand_dims(y, axis=0)

        logger.info(
            f"Batch {i}: {sample.batch_tensor.shape} - Pinned?: {sample.batch_tensor.is_pinned()} - dtype: {sample.batch_tensor.dtype} - device: {sample.batch_tensor.device} - global_coords: {global_coord_pos} - Pred shape: {y.shape}"
        )

    end_time = time()

    logger.info(f"Processing time: {end_time - start_time} seconds")

    # Getting tracked resources and plotting image
    utils.stop_child_process(profile_process)

    if len(time_points):
        utils.generate_resources_graphs(
            time_points,
            cpu_percentages,
            memory_usages,
            results_folder,
            "cellpose_segmentation",
        )


def predict_gradients(
    dataset_path: PathLike,
    multiscale: str,
    output_gradients_path: PathLike,
    slices_per_axis: List[int],
    target_size_mb: int,
    n_workers: int,
    batch_size: int,
    super_chunksize: Optional[Tuple[int, ...]] = None,
    normalize_image: Optional[bool] = True,
    model_name: Optional[str] = "cyto",
    cell_diameter: Optional[int] = 15,
):
    """
    Large-scale cellpose prediction of gradients.
    We estimate the gradients using entire 2D planes in
    XY, ZX and ZY directions. Cellpose is in nature a 2D
    network, therefore, there is no degradation in the
    prediction. We save these gradient estimation in
    each axis in a zarr dataset.

    Parameters
    ----------
    dataset_path: PathLike
        Path where the dataset in Zarr format is located.
        If the data is in the cloud, please provide the
        path to it. E.g., s3://bucket-name/path/image.zarr

    multiscale: str
        Dataset name insize the zarr dataset. If the zarr
        dataset is not organized in a folder structure,
        please use '.'

    output_gradients_path: PathLike
        Path where we want to output the estimated gradients
        in each plane.

    slices_per_axis: int
        Number of slices that will be pulled each time
        per axis. This should be set up.

    target_size_mb: int
        Parameter used to load a super chunk from the zarr dataset.
        This improves i/o operations and should be bigger than the
        prediction chunksize. Please, verify the amount of available
        shared memory in your system to set this parameter.

    n_workers: int
        Number of workers that will be pulling data from the
        super chunk.

    batch_size: int
        Number of prediction chunksize blocks that will be pulled
        per worker

    super_chunksize: Optional[Tuple[int, ...]]
        Super chunk size. Could be None if target_size_mb is provided.
        Default: None

    normalize_image: Optional[bool] = True
        If we want to normalize the data for cellpose.

    model_name: Optional[str] = "cyto"
        Model name to be used by cellpose

    cell_diameter: Optional[int] = 15
        Cell diameter for cellpose

    """

    # Reading image shape
    image_shape = zarr.open(f"{dataset_path}/{multiscale}", "r").shape

    axes_names = ["XY", "ZX", "ZY"]

    # Processing each plane at a time. This could be faster if you have more
    # GPUs, we are currently running this on a single GPU machine.
    for axis in range(0, 3):

        slice_per_axis = slices_per_axis[axis]
        prediction_chunksize = None

        # Setting prediction chunksize to entire planes using the number of slices per axis
        if axis == 0:
            prediction_chunksize = (slice_per_axis, image_shape[-2], image_shape[-1])

        elif axis == 1:
            prediction_chunksize = (image_shape[-3], slice_per_axis, image_shape[-1])

        elif axis == 2:
            prediction_chunksize = (image_shape[-3], image_shape[-2], slice_per_axis)

        print(
            f"{20*'='} Large-scale computation of gradients in {axes_names[axis]} - Prediction chunksize {prediction_chunksize} {20*'='}"
        )

        large_scale_cellpose_gradients_per_axis(
            dataset_path=dataset_path,
            multiscale=multiscale,
            output_gradients_path=output_gradients_path,
            axis=axis,
            prediction_chunksize=prediction_chunksize,
            target_size_mb=target_size_mb,
            n_workers=n_workers,
            batch_size=batch_size,
            super_chunksize=super_chunksize,
            normalize_image=normalize_image,
            model_name=model_name,
            cell_diameter=cell_diameter,
        )


def main():
    """
    Main function
    """
    BUCKET_NAME = "aind-open-data"
    IMAGE_PATH = "HCR_BL6-000_2023-06-1_00-00-00_fused_2024-02-09_13-28-49"
    TILE_NAME = "channel_405.zarr"
    # dataset_path = f"s3://{BUCKET_NAME}/{IMAGE_PATH}/{TILE_NAME}"
    dataset_path = f"/data/{IMAGE_PATH}/{TILE_NAME}"

    # Data loader params
    super_chunksize = None
    target_size_mb = 3072  # None
    n_workers = 16
    batch_size = 1

    # Cellpose params
    model_name = "cyto"
    normalize_image = True  # TODO Normalize image in the entire dataset
    cell_diameter = 15

    slices_per_axis = [40, 80, 80]

    # output gradients
    output_gradients_path = "../results/gradients.zarr"

    # Large-scale prediction of gradients
    predict_gradients(
        dataset_path=dataset_path,
        multiscale="2",
        output_gradients_path=output_gradients_path,
        slices_per_axis=slices_per_axis,
        target_size_mb=target_size_mb,
        n_workers=n_workers,
        batch_size=batch_size,
        super_chunksize=super_chunksize,
        normalize_image=normalize_image,
        model_name=model_name,
        cell_diameter=cell_diameter,
    )


if __name__ == "__main__":
    main()
import threading
from pathlib import Path
from functools import partial
from dataclasses import dataclass
from typing import Union, Optional
import xarray as xr
import numpy as np
from affine import Affine
import rasterio
from uuid import uuid4
from rasterio.windows import Window
from rasterio import MemoryFile
from rasterio.shutil import copy as rio_copy

NodataType = Union[int, float]


def roundup16(x):
    return (x + 15) & (~0xF)


def _adjust_blocksize(block, dim):
    if block > dim:
        return roundup16(dim)
    return roundup16(block)


def roi_shrink2(idx):
    def maybe_div2(x):
        if x is None:
            return None
        return x // 2

    if isinstance(idx, int):
        return idx // 2
    if isinstance(idx, slice):
        return slice(maybe_div2(idx.start), maybe_div2(idx.stop), maybe_div2(idx.step))

    return tuple(roi_shrink2(i) for i in idx)


@dataclass
class GeoRasterInfo:
    width: int
    height: int
    count: int
    dtype: str
    crs: str
    transform: Affine
    nodata: Optional[NodataType] = None

    def to_dict(self):
        out = dict(**self.__dict__)
        if self.nodata is None:
            out.pop("nodata")
        return out

    def raster_size(self) -> int:
        """
        Compute raster size in bytes
        """
        return np.dtype(self.dtype).itemsize * self.width * self.height * self.count

    @staticmethod
    def from_xarray(xx: xr.DataArray) -> "GeoRasterInfo":
        geobox = getattr(xx, "geobox", None)
        if geobox is None:
            raise ValueError("Missing .geobox on input array")

        height, width = geobox.shape
        if xx.ndim == 2:
            count = 1
        elif xx.ndim == 3:
            if xx.shape[:2] == (height, width):
                count = xx.shape[0]
            elif xx.shape[1:] == (height, width):
                count = xx.shape[2]
            else:
                raise ValueError("Geobox shape does not match array size")

        nodata = getattr(xx, "nodata", None)

        return GeoRasterInfo(
            width,
            height,
            count,
            xx.dtype.name,
            str(geobox.crs),
            geobox.transform,
            nodata,
        )

    def shrink2(self) -> "GeoRasterInfo":
        return GeoRasterInfo(
            width=self.width // 2,
            height=self.height // 2,
            count=self.count,
            dtype=self.dtype,
            crs=self.crs,
            transform=self.transform * Affine.scale(2, 2),
            nodata=self.nodata,
        )


class TIFFSink:
    def __init__(
        self,
        info: GeoRasterInfo,
        dst: Union[str, MemoryFile],
        blocksize: Optional[int] = None,
        bigtiff="auto",
        lock=True,
        **extra_rio_opts,
    ):
        if blocksize is None:
            blocksize = 512

        if bigtiff == "auto":
            # do bigtiff if raw raster is larger than 4GB
            bigtiff = info.raster_size() > (1 << 32)

        opts = dict(
            driver="GTiff",
            bigtiff=bigtiff,
            tiled=True,
            blockxsize=_adjust_blocksize(blocksize, info.width),
            blockysize=_adjust_blocksize(blocksize, info.height),
            compress="DEFLATE",
            zlevel=6,
            predictor=2,
            num_threads="ALL_CPUS",
        )
        opts.update(info.to_dict())
        opts.update(extra_rio_opts)

        mem: Optional[MemoryFile] = None
        self._mem_mine: Optional[MemoryFile] = None

        if isinstance(dst, str):
            if dst == ":mem:":
                mem = MemoryFile()
                out = mem.open(**opts)
                self._mem_mine = mem
            else:
                out = rasterio.open(dst, mode="w", **opts)
        else:
            mem = dst
            out = dst.open(**opts)

        self._mem = mem
        self._info = info
        self._out = out
        self._lock = threading.Lock() if lock else None

    def __str__(self):
        ii = self._info
        return f"TIFFSink: {ii.width}x{ii.height}..{ii.count}..{ii.dtype}"

    def __repr__(self):
        return self.__str__()

    @property
    def name(self):
        return self._out.name

    @property
    def info(self):
        return self._info

    def close(self):
        self._out.close()

    def __del__(self):
        self.close()

        if self._mem_mine:
            self._mem_mine.close()
            self._mem_mine = None

    def __setitem__(self, key, item):
        ndim = len(key)
        info = self._info
        assert ndim in (2, 3)

        if ndim == 2:
            assert item.ndim == 2
            band, block = 1, item
            win = Window.from_slices(*key, height=info.height, width=info.width)
        elif ndim == 3:
            # TODO: figure out which dimension is "band" and which bands are being written to
            raise NotImplementedError()  # TODO:
        else:
            raise ValueError("Only accept 2 and 3 dimensional data")

        if self._lock:
            with self._lock:
                self._out.write(block, indexes=band, window=win)
        else:
            self._out.write(block, indexes=band, window=win)


class COGSink:
    def __init__(
        self,
        info: GeoRasterInfo,
        dst: str,
        blocksize: Optional[int] = None,
        ovr_blocksize: Optional[int] = None,
        bigtiff: Union[bool, str] = "auto",
        lock: bool = True,
        temp_folder: Optional[str] = None,
        **extra_rio_opts,
    ):
        if blocksize is None:
            blocksize = 512

        if ovr_blocksize is None:
            ovr_blocksize = blocksize

        if bigtiff == "auto":
            # do bigtiff if raw raster is larger than 4GB
            bigtiff = info.raster_size() > (1 << 32)

        opts = dict(
            driver="GTiff",
            bigtiff=bigtiff,
            tiled=True,
            blockxsize=_adjust_blocksize(blocksize, info.width),
            blockysize=_adjust_blocksize(blocksize, info.height),
            compress="DEFLATE",
            zlevel=6,
            predictor=2,
            num_threads="ALL_CPUS",
        )
        opts.update(extra_rio_opts)

        rio_opts_temp = dict(
            compress="zstd",
            zstd_level=1,
            predictor=1,
            num_threads="ALL_CPUS",
            sparse_ok=True,
        )
        layers = []
        temp = str(uuid4())
        if temp_folder:
            temp_folder = Path(temp_folder)
            t_dir, t_name = temp_folder, temp
        else:
            t_dir, t_name = temp[:8], temp[9:]

        ext = ".tif"
        ii = info
        bsz = 2048
        for _ in range(7 + 1):
            if temp_folder:
                _dst = str(temp_folder/f"{t_name}{ext}")
            else:
                _dst = MemoryFile(dirname=t_dir, filename=t_name + ext)
            sink = TIFFSink(
                ii, _dst, lock=lock, blocksize=bsz, bigtiff=bigtiff, **rio_opts_temp
            )
            layers.append(sink)

            # If last overview was odd sized do no more
            if (ii.width % 2) or (ii.height % 2):
                break

            # If last overview was smaller than 1 block along any dimension don't
            # go further
            if min(ii.width, ii.height) < ovr_blocksize:
                break

            ii = ii.shrink2()
            ext = ext + ".ovr"
            if bsz > 64:
                bsz = bsz // 2

        self._layers = layers
        self._dst = dst
        self._rio_opts = opts
        self._ovr_blocksize = ovr_blocksize

    def shrink2(self, xx, roi):
        # TODO: make it user configurable
        # TODO: what happens if input has odd size?

        wk_dtype = "int32" if xx.dtype.itemsize <= 2 else xx.dtype
        roi = roi_shrink2(roi)

        temp = xx[0::2, :].astype(wk_dtype) + xx[1::2, :].astype(wk_dtype)
        temp = temp[:, 0::2] + temp[:, 1::2]
        return roi, (temp / 4).astype(xx.dtype)

    def __setitem__(self, key, item):
        dst, *ovrs = self._layers
        dst[key] = item
        for dst in ovrs:
            key, item = self.shrink2(item, key)
            dst[key] = item

    def close(self):
        for dst in self._layers:
            dst.close()

    def finalise(self):
        self.close()  # Write out any remainders if needed

        with rasterio.Env(GDAL_TIFF_OVR_BLOCKSIZE=self._ovr_blocksize):
            src = self._layers[0].name
            rio_copy(src, self._dst, copy_src_overviews=True, **self._rio_opts)

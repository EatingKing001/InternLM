#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import asyncio
import concurrent.futures
import hashlib
import io
import os
import pickle
import re
import socket
import stat
from asyncio import InvalidStateError
from asyncio.tasks import ALL_COMPLETED
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Union

import boto3
import botocore
import torch
import torch.distributed as dist

from internlm.core.context import global_context as gpc
from internlm.utils.common import SingletonMeta
from internlm.utils.logger import get_logger

logger = get_logger(__file__)

boto3_url_re = re.compile(r"([^\.]+)\.([\d\.]+)")

MB = 1024**2

storage_manager = None


def check_folder(fp: str):
    storage_manager.assert_fp_exists(fp)


def get_fns(fp: str):
    return storage_manager.get_fns(fp)


def llm_load(fp: str, *args, **kwargs):
    return storage_manager.load(fp, *args, **kwargs)


def llm_save(save_path: str, saved_obj: Any, *args, **kwargs):
    storage_manager.save(save_path, *args, saved_obj=saved_obj, **kwargs)


class StorageClient:
    """
    StorageClient as a client for s3 storage access.
    """

    def __init__(self, handler) -> None:
        self.handler = handler

    @staticmethod
    def load(client, load_path: str, *args, **kwargs):
        raise NotImplementedError

    @staticmethod
    def sync_upload_fileobj(*args, saved_obj=None, **kwargs):
        raise NotImplementedError

    @staticmethod
    def assert_fp_exists(client):
        raise NotImplementedError

    @staticmethod
    def get_fns(client):
        raise NotImplementedError


class Boto3MetaInfo:
    """Boto3 meta info for save/load etc."""

    def __init__(
        self,
        is_async,
        handler: StorageClient,
        bucket_name: str,
        endpoint: str,
        file_path: str,
        async_upload_fn: callable,
        local_nvme_path=None,
    ) -> None:
        self.is_async = is_async
        self.client = handler
        self.bucket_name = bucket_name
        self.endpoint = endpoint
        self.file_path = file_path
        self.async_upload_fn = async_upload_fn
        self.local_nvme_path = local_nvme_path

    def __str__(self) -> str:
        return f"is_async: {self.is_async}, bucket_name:{self.bucket_name}, endpoint:{self.endpoint}, \
local_nvme_path: {self.local_nvme_path}"


class LocalMetaInfo:
    """Local meta info for save/load etc."""

    def __init__(self, handler: StorageClient, dest_path: str) -> None:
        self.is_async = False
        self.client = handler
        self.dest_path = dest_path
        self.async_upload_fn = None


def unpack_meta(meta):
    args = []
    is_async = meta.is_async
    for k, v in meta.__dict__.items():
        if k in ("endpoint", "async_upload_fn", "is_async"):
            continue
        if not is_async and k in ("local_nvme_path",):
            continue
        args.append(v)

    return args


def compute_file_md5_by_chunk(file_name: str):
    hash_md5 = hashlib.md5()
    with open(file_name, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


class Boto3Client(StorageClient):
    """
    Boto3Client
    """

    def __init__(
        self,
        s3_endpoint_url: str,
        use_threads: int = True,
        multipart_chunksize=8 * MB,
        max_concurrency: int = 10,
        multipart_threshold=100 * MB,
    ) -> None:
        """S3 object/file storage management class

        Args:
            s3_access_keys_id (str): S3 access key ID.
            s3_secret_access_key (str): S3 secret access key.
            use_threads (bool, optional): Whether to enable multipart. Defaults to True.
            multipart_chunksize (_type_, optional): Defaults to 8*MB.
            max_concurrency (int, optional): Defaults to 10.

        Raises:
            RuntimeError: Connection failures caused by misconfiguration or network problems.
        """
        super().__init__(boto3)
        self.botocore = botocore
        try:
            s3_access_key_id = os.environ["S3_ACCESS_KEY_ID"]
            s3_secret_access_key = os.environ["S3_SECRET_ACCESS_KEY_ID"]
        except KeyError as exc:
            raise RuntimeError(
                "Please set boto3 bucket 'S3_ACCESS_KEY_ID' and 'S3_SECRET_ACCESS_KEY_ID' using environment variable!"
            ) from exc

        self.client = self.handler.client(
            "s3",
            "",
            use_ssl=False,
            verify=False,
            endpoint_url=s3_endpoint_url,
            aws_access_key_id=s3_access_key_id,
            aws_secret_access_key=s3_secret_access_key,
        )

        self.config = self.handler.s3.transfer.TransferConfig(
            multipart_threshold=multipart_threshold,
            max_concurrency=max_concurrency,
            multipart_chunksize=multipart_chunksize,
            use_threads=use_threads,
        )

    @staticmethod
    def sync_upload_fileobj(
        handler, bucket_name: str, fp: str, local_nvme_path: str, *args, saved_obj=None, **kwargs
    ):  # pylint: disable=W0613
        assert saved_obj is not None, "saved_obj is None!"
        try:
            with io.BytesIO() as f:
                torch.save(saved_obj, f, *args, **kwargs)
                f.seek(0)
                handler.client.upload_fileobj(f, bucket_name, fp, Config=handler.config)
        except handler.botocore.exceptions.EndpointConnectionError as exc:
            raise RuntimeError(
                f"Boto3 Network Error: Please Check your Internet Connection in {socket.gethostname()}"
            ) from exc

    @staticmethod
    def load(
        handler,
        bucket_name: str,
        fp: str,
        local_nvme_path: str,  # pylint: disable=W0613
        *args,
        **kwargs,
    ) -> Dict:
        """
        Args:
            fp (str): Path to save, eg. s3://opennlplab/model_weights/xxx/ddd.pt
        """
        try:
            with io.BytesIO() as f:
                handler.client.download_fileobj(bucket_name, fp, f, Config=handler.config)
                f.seek(0)
                states = torch.load(f, *args, **kwargs)
        except handler.botocore.exceptions.EndpointConnectionError as exc:
            raise RuntimeError(
                f"Boto3 Network Error: Please Check your Internet Connection in {socket.gethostname()}"
            ) from exc
        return states

    @staticmethod
    def assert_fp_exists(handler, bucket_name: str, fp: str, local_nvme_path: str):  # pylint: disable=W0613
        assert len(list(handler.client.list_objects(Bucket=bucket_name, Prefix=fp)["Contents"])) > 0, fp

    @staticmethod
    def get_fns(handler, bucket_name: str, fp: str, local_nvme_path: str, *args, **kwargs):  # pylint: disable=W0613
        """
        Ref: https://stackoverflow.com/questions/54314563/
        how-to-get-more-than-1000-objects-from-s3-by-using-list-objects-v2
        """
        paginator = handler.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket_name, Prefix=fp)

        folder_name_list = []
        for page in pages:
            for obj in page["Contents"]:
                fp: str = obj["Key"]
                folder_name_list.append(fp.rsplit("/", maxsplit=1)[1])
        return folder_name_list

    @staticmethod
    def async_upload_fileobj(handler, bucket_name: str, fp: str, local_nvme_path: str):
        try:
            with open(local_nvme_path, "rb") as f:
                handler.client.upload_fileobj(f, bucket_name, fp, Config=handler.config)
        except handler.botocore.exceptions.EndpointConnectionError as exc:
            raise RuntimeError(
                f"Boto3 Network Error: Please Check your Internet Connection in {socket.gethostname()}"
            ) from exc
        except Exception as e:
            raise e

    @staticmethod
    def delete_obj(handler, fp: str):
        raise NotImplementedError("boto3 not support delete_obj")


class LocalClient(StorageClient):
    """
    Storage Client for local NFS.
    """

    def __init__(self, *args, **kwargs) -> None:  # pylint: disable=W0613
        super().__init__(None)

    @staticmethod
    def sync_upload_fileobj(handler, fp: str, *args, saved_obj=None, **kwargs):
        assert isinstance(handler, LocalClient)
        assert saved_obj is not None
        fp_dirname = os.path.dirname(fp)
        if not os.path.exists(fp_dirname):
            os.makedirs(fp_dirname, exist_ok=True)
        torch.save(saved_obj, fp, *args, **kwargs)

    @staticmethod
    def load(handler, fp: str, *args, **kwargs):  # pylint: disable=W0613
        assert isinstance(handler, LocalClient)
        assert os.path.exists(fp), f"{fp} is not found!"
        with open(fp, "rb") as f:
            states = torch.load(f, *args, **kwargs)
        return states

    @staticmethod
    def assert_fp_exists(handler, folder):
        assert isinstance(handler, LocalClient)
        assert os.path.exists(folder), folder

    @staticmethod
    def get_fns(handler, folder):
        assert isinstance(handler, LocalClient)
        assert os.path.exists(folder), f"folder '{folder}' not exists!"
        fns = os.listdir(folder)
        return fns

    @staticmethod
    def delete_obj(handler, fp: str):
        assert isinstance(handler, LocalClient)
        if not os.path.isdir(fp):
            os.remove(fp)


def get_tmp_file_name(tmp_local_folder: str, fp: str):
    """
    It should be noted that all our temporary files will be stored in the same folder,
    so the file name passed upstream must be unique.
    """
    base_path = os.path.join(tmp_local_folder, fp.split("/")[-1])
    current_time = datetime.now().strftime("%b%d_%H-%M-%S")
    pid = os.getpid()
    # step = self.step_counter
    return "-".join([base_path, current_time, str(pid)]) + ".tmpfile"  # , str(step)


def get_boto3_meta(fp: str, tmp_local_folder: str, is_async: bool) -> Boto3MetaInfo:
    assert fp.startswith("s3://"), f"Path '{fp}' is not a boto3 url"
    parts = fp.lstrip("s3://").split(os.path.sep)
    match = boto3_url_re.match(parts[0])
    assert match is not None, f"url '{fp}' is not a valid boto3 url"
    bucket_name, endpoint = match.group(1), match.group(2)
    endpoint = "http://" + endpoint + ":80"
    tmp_step_file = get_tmp_file_name(tmp_local_folder, fp)
    return Boto3MetaInfo(
        is_async=is_async,
        handler=None,
        bucket_name=bucket_name,
        endpoint=endpoint,
        file_path=os.path.sep.join(parts[1:]),
        async_upload_fn=Boto3Client.async_upload_fileobj,
        local_nvme_path=tmp_step_file,
    )


def get_local_meta(fp: str) -> LocalMetaInfo:
    assert not fp.startswith("s3://"), f"Path '{fp}' is not a local path"
    return LocalMetaInfo(None, fp)


def get_mount_point_free_size(path: str):
    """
        Returns the remaining space of the temporary storage mount point as a percentage.
    Args:
        path (str): temporary storage folder path.

    Raises:
        FileNotFoundError: If the temporary storage folder does not exist,
        an error will be reported。
    """
    if os.path.exists(path):
        st = os.statvfs(path)
        # f_bavail: Number of free blocks for unprivileged users.
        # f_bsize: Filesystem block size.
        # return unit is TB.
        return st.f_bavail * st.f_bsize / (1024**3)


def check_tmp_folder_accessibility(tmp_local_folder: str):
    """
    Check access permissions for temporary storage.
    """
    ret = True
    if os.path.exists(tmp_local_folder):
        ret &= os.access(tmp_local_folder, os.W_OK)
        ret &= os.access(tmp_local_folder, os.R_OK)
        if ret is False:
            error_str = f'{socket.gethostname()} dose not have read and write permissions on {tmp_local_folder}"'
            raise RuntimeError(error_str)


class StorageManager(metaclass=SingletonMeta):
    """
    Storage Manager for saving or loading checkpoint.
    TODO: add a thread to poll the asynchronous storage state.
    """

    BACKEND_TYPE = {"boto3", "local"}
    BACKEND_INIT_METHOD = {
        "boto3": Boto3Client,
        "local": LocalClient,
    }
    CLI_DICT = {}

    def __init__(self, enable_save, tmp_local_folder="/dev/shm/test/", async_mode=True, n_async_workers=8) -> None:
        self._exception_list = []
        self._to_be_del_files = []
        self._async_stack = []
        self.upload_count = 0
        self.tmp_local_folder = tmp_local_folder
        self.async_mode = async_mode
        self.has_warning = False

        if enable_save and self.async_mode:
            self._async_loop = asyncio.new_event_loop()
            self._thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=n_async_workers)

            check_tmp_folder_accessibility(os.path.dirname(self.tmp_local_folder))

            # Try to create tmp folder
            try:
                os.makedirs(self.tmp_local_folder, exist_ok=True)
                os.chmod(self.tmp_local_folder, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            except FileExistsError:
                pass

            # In case it is a directory created by other users, we check the permissions again.
            check_tmp_folder_accessibility(self.tmp_local_folder)

            # Try to clean tmp folder's empty folder.
            self.try_delete_tmpfile(self.tmp_local_folder)

            # Avaliable storeage space check.
            free_size = get_mount_point_free_size(self.tmp_local_folder)
            if free_size < 0.1:
                logger.error(f'tmp_local_folder only have "{free_size}" GB free space, less then 100 GB!')
                raise RuntimeError(f"Insufficient temporary storage space on {socket.gethostname()}")

    def _get_client(self, path=str) -> Union[Boto3MetaInfo, LocalMetaInfo]:
        """
        example:
        local:/path/to/checkpoint
        boto3:s3://model_weights/0331/120bi

        Args:
            path (str): _description_
        """
        try:
            backend, path = path.split(":", maxsplit=1)
        except Exception as exc:
            raise AttributeError(f"Given path '{path}' is not startwith backend prefix:'local/boto3'") from exc

        init_args = (None,)
        if backend == "local":
            meta_info = get_local_meta(path)
            backend_key = backend
        elif backend == "boto3":
            meta_info = get_boto3_meta(path, self.tmp_local_folder, self.async_mode)
            backend_key = backend + ":" + meta_info.endpoint
            init_args = (meta_info.endpoint,)
            if (
                "http_proxy" in os.environ
                or "https_proxy" in os.environ
                or "HTTP_PROXY" in os.environ
                or "HTTPS_PROXY" in os.environ
            ):
                if not self.has_warning:
                    logger.warning(
                        "HTTP/HTTPS proxy is detected when using boto3, incorrectly setting \
    the proxy may make boto3 unavailable or affect performance."
                    )
                    self.has_warning = True

        assert backend in StorageManager.BACKEND_TYPE, f"Unkown backend: {backend}"

        # boto3 backend need special treatment.
        if backend_key not in StorageManager.CLI_DICT:
            StorageManager.CLI_DICT.update({backend_key: StorageManager.BACKEND_INIT_METHOD[backend](*init_args)})

        meta_info.client = StorageManager.CLI_DICT[backend_key]

        return meta_info

    def assert_fp_exists(self, folder) -> None:
        meta = self._get_client(path=folder)
        meta.client.assert_fp_exists(*unpack_meta(meta))

    def get_fns(self, folder) -> List[str]:
        meta = self._get_client(path=folder)
        return meta.client.get_fns(*unpack_meta(meta))

    def save(self, save_path: str, saved_obj: Any, *args, async_upload=None, **kwargs):
        meta = self._get_client(path=save_path)

        if async_upload is None:
            async_upload = self.async_mode
        if async_upload:
            assert (
                self.tmp_local_folder
            ), "StorageManager is not setted tmp_local_folder, so async save cannot be performed."
            tmp_step_file = meta.local_nvme_path
            self._to_be_del_files.append(tmp_step_file)
            with open(tmp_step_file, "wb") as f:
                torch.save(saved_obj, f, pickle_protocol=pickle.HIGHEST_PROTOCOL)
            self.async_executor(meta.async_upload_fn, *unpack_meta(meta))
            os.chmod(tmp_step_file, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        else:
            meta.client.sync_upload_fileobj(*unpack_meta(meta), *args, saved_obj=saved_obj, **kwargs)
            self.upload_count += 1

    def load(self, load_path: str, *args, **kwargs) -> Any:
        self.wait()
        meta = self._get_client(path=load_path)
        return meta.client.load(*unpack_meta(meta), *args, **kwargs)

    def delete_obj(self, fp: str):
        meta = self._get_client(path=fp)
        meta.client.delete_obj(*unpack_meta(meta))

    def _del_tmp_folder(self):
        for fp in self._to_be_del_files:
            try:
                os.remove(fp)
            except FileNotFoundError:
                pass
            except SystemError as e:
                logger.error(f'delete file: {fp}, failed for reason:"{e}"')
            else:
                pass

    def try_delete_tmpfile(self, tmp_dir: str):
        """Delete temporary files in tmp_dir."""

        for filename in os.listdir(tmp_dir):
            if filename.endswith(".tmpfile"):
                file_path = os.path.join(tmp_dir, filename)
                try:
                    os.remove(file_path)
                    logger.info(f"Delete tmpfile: {file_path}")
                except OSError:
                    # Ignore deletion errors
                    pass

    async def _sync_tasks(self) -> Awaitable[None]:
        if not self._async_stack:
            return

        await asyncio.wait(self._async_stack, return_when=ALL_COMPLETED)

        for task in self._async_stack:
            try:
                task.exception()
            except InvalidStateError:
                continue
            except Exception as e:
                file_id = len(self._exception_list)
                self._exception_list.append((e, file_id))

                logger.error(f"File: {self._to_be_del_files[file_id]}, " f"upload failed with {e}")

        self._async_stack.clear()

    def async_executor(self, fn: Callable, *args, **kwargs) -> None:
        """
        Overview:
            Execute task in background, then apppend the future instance in _async_stack.
        Arguments:
            - fn (:obj:`Callable`): Synchronization fuction.
        """
        if not self._async_loop:
            raise RuntimeError("Event loop was not initialized, please call this function in async or parallel mode")
        t = self._async_loop.run_in_executor(self._thread_pool, fn, *args, **kwargs)
        self._async_stack.append(t)

    def wait(self) -> bool:
        """Wait for async operations to complete."""

        if not self.async_mode:
            return

        if self._async_loop:
            self._async_loop.run_until_complete(self._sync_tasks())

        if self._exception_list:
            for file_id, error_msg in self._exception_list:
                logger.error(
                    f"Node:{socket.gethostname()}, Error: Checkpoint {self._to_be_del_files[file_id]} "
                    f"failed on step {self.upload_count}: {error_msg}"
                )

                # TODO: Re-upload in sync mode
                raise RuntimeError(
                    f"Failed to upload {self._to_be_del_files[file_id]} " f"on step {self.upload_count}: {error_msg}"
                )

        self._del_tmp_folder()
        self._exception_list.clear()
        self._to_be_del_files.clear()

        if gpc.is_rank_for_log():
            logger.info("all async uploads succeeded!")
            self.upload_count += 1


storage_manager: StorageManager = None


def init_storage_manager(ckpt_config):
    global storage_manager
    storage_manager = StorageManager(
        ckpt_config.enable_save_ckpt,
        tmp_local_folder=ckpt_config.async_upload_tmp_folder,
        async_mode=ckpt_config.async_upload,
    )


def get_storage_manager():
    assert storage_manager is not None, "storage_manager has not been init!"
    return storage_manager


def wait_async_upload_finish():
    dist.barrier()
    storage_manager.wait()

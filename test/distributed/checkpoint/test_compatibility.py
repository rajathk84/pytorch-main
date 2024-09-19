# Owner(s): ["oncall: distributed"]

from unittest.mock import patch

import torch
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.metadata import (
    BytesStorageMetadata,
    ChunkStorageMetadata,
    Metadata,
    MetadataIndex,
    TensorProperties,
    TensorStorageMetadata,
)
from torch.testing._internal.common_utils import run_tests, TestCase
from torch.testing._internal.distributed.checkpoint_utils import with_temp_dir


class TestDCPCompatbility(TestCase):
    def test_metadata(self) -> None:
        # Ensure that all the new fields of all the metadata have the default
        # values so that we can always deserialize from a legacy metadata.
        try:
            tensor = torch.zeros(4, 4)
            chunk_meta = ChunkStorageMetadata(
                torch.Size((1, 1)),
                torch.Size((1, 1)),
            )
            tensor_meta = TensorStorageMetadata(
                properties=TensorProperties.create_from_tensor(tensor),
                size=tensor.size(),
                chunks=[chunk_meta],
            )
            b_meta = BytesStorageMetadata()
            _ = Metadata(state_dict_metadata={"a": tensor_meta, "b": b_meta})

            _ = MetadataIndex(fqn="a.b.c")
        except Exception as e:
            raise RuntimeError(
                "The change may break the BC of distributed checkpoint."
            ) from e

    def test_sharded_tensor_dependency(self) -> None:
        # Ensure that we can load the existing DCP checkpoints back even if the
        # metadata contain # _shard.sharded_tensor.metadata.
        from torch.distributed._shard.sharded_tensor.metadata import (
            TensorProperties as stp,
        )

        with patch("torch.distributed.checkpoint.metadata.TensorProperties", stp):
            dcp.save(
                {"a": torch.zeros(4, 4)},
                dcp.FileSystemWriter("/tmp/dcp_testing"),
            )

        dcp.load(
            {"a": torch.zeros(4, 4)},
            dcp.FileSystemReader("/tmp/dcp_testing"),
        )

    @with_temp_dir
    def test_storage_meta(self) -> None:
        writer = dcp.FileSystemWriter(self.temp_dir)
        dcp.save({"a": torch.zeros(4, 4)}, storage_writer=writer)

        reader = dcp.FileSystemReader(self.temp_dir)
        storage_meta = reader.read_metadata().storage_meta
        self.assertNotEqual(storage_meta, None)
        self.assertEqual(str(storage_meta.checkpoint_id), self.temp_dir)
        self.assertEqual(storage_meta.save_id, writer.save_id)
        self.assertEqual(storage_meta.load_id, reader.load_id)

    @with_temp_dir
    def test_with_v_2_3(self) -> None:
        sd = {
            "a": torch.zeros(4, 4),
            "dict": {
                "dict_a": {"dict_a_1": 1, "dict_a_2": 2},
                "dict_b": {"dict_b_1": 1, "dict_b_2": 2},
            },
            "list": [0, 1, 2, 3, 4, 5],
        }
        load_sd = {
            "a": torch.ones(4, 4),
            "dict": {
                "dict_a": {"dict_a_1": 2, "dict_a_2": 4},
                "dict_b": {"dict_b_1": 2, "dict_b_2": 4},
            },
            "list": [10, 11, 12, 13, 14, 15],
        }

        dcp._version._act_like_version = "2_3"
        dcp.save(sd, checkpoint_id=self.temp_dir)
        dcp._version._act_like_version = None
        dcp.load(load_sd, checkpoint_id=self.temp_dir)
        self.assertEqual(sd, load_sd)


if __name__ == "__main__":
    run_tests()

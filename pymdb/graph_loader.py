# NOTE: The uint64 vectors are parsed as int64 vectors due the lack of the uint64 dtype
# in the torch.Tensor class.

import abc
from typing import TYPE_CHECKING, List

import torch

from .protocol import RequestType, StatusCode
from .utils import decorators, packer

if TYPE_CHECKING:
    from .mdb_client import MDBClient

## Data structure to represent a graph, used by the GraphLoader class.
#
# The `node_ids` are relabelled to be consecutive integers starting from 0. The
# `seed_ids` are always at the first `num_seeds` on the `node_ids` list.
class Graph:
    ## Constructor.
    def __init__(
        self,
        node_features: torch.Tensor,
        node_labels: torch.Tensor,
        edge_index: torch.Tensor,
        node_ids: List[int],
        num_seeds: int,
        feature_size: int,
    ) -> None:
        ## Node features of shape `[num_nodes, feature_size]`
        self.node_features = node_features
        ## Node labels of shape `[num_nodes]`
        self.node_labels = node_labels
        ## Edge index of shape `[2, num_edges]`
        self.edge_index = edge_index
        ## Node ids of shape `[num_nodes]`
        self.node_ids = node_ids
        ## Number of seed ids used to generate the graph.
        self.num_seeds = num_seeds
        ## Feature size of the node features
        self.feature_size = feature_size

    ## Class representation.
    def __repr__(self) -> str:
        return (
            "Graph("
            + f"node_features={list(self.node_features.size())}, "
            + f"node_labels={list(self.node_labels.size())}, "
            + f"edge_index={list(self.edge_index.size())}, "
            + f"node_ids=[{len(self.node_ids)}], "
            + f"num_seeds={self.num_seeds}, "
            + f"feature_size={self.feature_size})"
        )


## Abstract class for the graph loader iterators.
class GraphLoader(abc.ABC):
    ## Constructor.
    @abc.abstractmethod
    def __init__(
        self,
        client: "MDBClient",
        tensor_store_name: str,
        batch_size: int,
        num_neighbors: List[int],
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if len(num_neighbors) == 0:
            raise ValueError("num_neighbors must be non-empty")
        ## Client instance.
        self.client = client
        ## Name of the TensorStore to load the features from.
        self.tensor_store_name = tensor_store_name
        ## Number of seeds to use on each iteration.
        self.batch_size = batch_size
        ## Number of neighbors to sample at each layer (negative values are interpreted
        # as all neighbors).
        self.num_neighbors = list(
            map(lambda x: 2**64 - 1 if x < 0 else x, num_neighbors)
        )

        self._graph_loader_id = None
        self._size = None
        self._closed = True

    @abc.abstractmethod
    def _new(self, *args) -> None:
        pass

    ## Class representation.
    @abc.abstractmethod
    def __repr__(self) -> str:
        pass

    ## Returns `True` if the GraphLoader is closed.
    def is_closed(self) -> bool:
        return self._closed

    ## Closes the GraphLoader.
    def close(self) -> None:
        if not self._closed:
            self._close()

    ## Returns the number of graphs that can be generated by the instance.
    def __len__(self) -> int:
        return self.size()

    ## Returns the number of graphs that can be generated by the instance.
    @decorators.check_closed
    def size(self) -> int:
        return self._size

    ## Initializes the iterator.
    @decorators.check_closed
    def __iter__(self) -> "GraphLoader":
        self._begin()
        return self

    ## Returns the next graph.
    @decorators.check_closed
    def __next__(self) -> Graph:
        # Send request
        msg = b""
        msg += packer.pack_byte(RequestType.GRAPH_LOADER_NEXT)
        msg += packer.pack_uint64(self._graph_loader_id)
        self.client._send(msg)

        # Handle response
        data, code = self.client._recv()

        if code == StatusCode.END_OF_ITERATION:
            raise StopIteration

        num_nodes = packer.unpack_uint64(data[0:8])
        num_edges = packer.unpack_uint64(data[8:16])
        num_seeds = packer.unpack_uint64(data[16:24])
        feature_size = packer.unpack_uint64(data[24:32])

        lo, hi = 32, 32 + 4 * num_nodes * feature_size
        node_features = torch.tensor(
            data=packer.unpack_float_vector(data[lo:hi]), dtype=torch.float32
        ).reshape(num_nodes, feature_size)

        lo, hi = hi, hi + 8 * num_nodes
        node_labels = torch.tensor(
            data=packer.unpack_uint64_vector(data[lo:hi]), dtype=torch.int64
        )

        lo, hi = hi, hi + 8 * 2 * num_edges
        edge_index = torch.tensor(
            data=packer.unpack_uint64_vector(data[lo:hi]), dtype=torch.int64
        ).reshape(2, num_edges)

        lo, hi = hi, hi + 8 * num_nodes
        node_ids = packer.unpack_uint64_vector(data[lo:hi])

        return Graph(
            node_features,
            node_labels,
            edge_index,
            node_ids,
            num_seeds,
            feature_size,
        )

    def _begin(self) -> None:
        # Send request
        msg = b""
        msg += packer.pack_byte(RequestType.GRAPH_LOADER_BEGIN)
        msg += packer.pack_uint64(self._graph_loader_id)
        self.client._send(msg)

        # Handle response
        self.client._recv()

    def _close(self) -> None:
        # Send request
        msg = b""
        msg += packer.pack_byte(RequestType.GRAPH_LOADER_CLOSE)
        msg += packer.pack_uint64(self._graph_loader_id)
        self.client._send(msg)

        # Handle response
        self.client._recv()
        self._graph_loader_id = None
        self._size = None
        self._closed = True


## GraphLoader for evaluation that performs mini-batching with the entire graph.
class EvalGraphLoader(GraphLoader):
    def __init__(
        self,
        client: "MDBClient",
        tensor_store_name: str,
        batch_size: int,
        num_neighbors: List[int],
    ) -> None:
        super().__init__(
            client=client,
            tensor_store_name=tensor_store_name,
            batch_size=batch_size,
            num_neighbors=num_neighbors,
        )
        self._new()

    def _new(self) -> None:
        # Send request
        msg = b""
        msg += packer.pack_byte(RequestType.EVAL_GRAPH_LOADER_NEW)
        msg += packer.pack_uint64(self.batch_size)
        msg += packer.pack_uint64(len(self.num_neighbors))
        msg += packer.pack_uint64(len(self.tensor_store_name))
        msg += packer.pack_uint64_vector(self.num_neighbors)
        msg += packer.pack_string(self.tensor_store_name)
        self.client._send(msg)

        # Handle response
        data, _ = self.client._recv()
        self._graph_loader_id = packer.unpack_uint64(data[0:8])
        self._size = packer.unpack_uint64(data[8:16])
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f'tensor_store_name="{self.tensor_store_name}", '
            f"batch_size={self.batch_size}, "
            f"num_neighbors={self.num_neighbors})"
        )


## GraphLoader that performs mini-batching sampling and generating a new set of seeds
# on each iterator initialization.
class SamplingGraphLoader(GraphLoader):
    def __init__(
        self,
        client: "MDBClient",
        tensor_store_name: str,
        batch_size: int,
        num_neighbors: List[int],
        num_seeds: int,
    ) -> None:
        if num_seeds < 1:
            raise ValueError("num_seeds must be a positive integer")
        super().__init__(
            client=client,
            tensor_store_name=tensor_store_name,
            batch_size=batch_size,
            num_neighbors=num_neighbors,
        )
        ## Number of seeds to generate on each iterator initialization.
        self.num_seeds = num_seeds
        self._new()

    def _new(self) -> None:
        # Send request
        msg = b""
        msg += packer.pack_byte(RequestType.SAMPLING_GRAPH_LOADER_NEW)
        msg += packer.pack_uint64(self.batch_size)
        msg += packer.pack_uint64(self.num_seeds)
        msg += packer.pack_uint64(len(self.num_neighbors))
        msg += packer.pack_uint64(len(self.tensor_store_name))
        msg += packer.pack_uint64_vector(self.num_neighbors)
        msg += packer.pack_string(self.tensor_store_name)
        self.client._send(msg)

        # Handle response
        data, _ = self.client._recv()
        self._graph_loader_id = packer.unpack_uint64(data[0:8])
        self._size = packer.unpack_uint64(data[8:16])
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f'tensor_store_name="{self.tensor_store_name}", '
            f"num_seeds={self.num_seeds}, "
            f'batch_size="{self.batch_size}", '
            f"num_neighbors={self.num_neighbors})"
        )


## GraphLoader that performs mini-batching sampling from a fixed set of seed ids.
class TrainGraphLoader(GraphLoader):
    def __init__(
        self,
        client: "MDBClient",
        tensor_store_name: str,
        batch_size: int,
        num_neighbors: List[int],
        seed_ids: List[int],
    ) -> None:
        super().__init__(
            client=client,
            tensor_store_name=tensor_store_name,
            batch_size=batch_size,
            num_neighbors=num_neighbors,
        )
        if len(seed_ids) == 0:
            raise ValueError("seed_ids must be non-empty")
        ## List of seed ids to sample from.
        self.seed_ids = seed_ids
        self._new()

    def _new(self) -> None:
        # Send request
        msg = b""
        msg += packer.pack_byte(RequestType.TRAIN_GRAPH_LOADER_NEW)
        msg += packer.pack_uint64(self.batch_size)
        msg += packer.pack_uint64(len(self.num_neighbors))
        msg += packer.pack_uint64(len(self.tensor_store_name))
        msg += packer.pack_uint64(len(self.seed_ids))
        msg += packer.pack_uint64_vector(self.num_neighbors)
        msg += packer.pack_string(self.tensor_store_name)
        msg += packer.pack_uint64_vector(self.seed_ids)
        self.client._send(msg)

        # Handle response
        data, _ = self.client._recv()
        self._graph_loader_id = packer.unpack_uint64(data[0:8])
        self._size = packer.unpack_uint64(data[8:16])
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f'tensor_store_name="{self.tensor_store_name}", '
            f'batch_size="{self.batch_size}", '
            f"num_neighbors={self.num_neighbors}, "
            f"num_seed_ids={len(self.seed_ids)})"
        )

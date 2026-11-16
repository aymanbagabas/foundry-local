# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
import logging
import sys

from foundry_local_sdk.catalog import Catalog, CatalogType
from foundry_local_sdk.configuration import Configuration
from foundry_local_sdk.ep_types import EpDownloadResult, EpInfo
from foundry_local_sdk.exception import FoundryLocalException
from foundry_local_sdk.foundry_local_manager import FoundryLocalManager
from foundry_local_sdk.imodel import IModel
from foundry_local_sdk.item_queue import ItemQueue
from foundry_local_sdk.items import (
    AudioItem,
    BytesItem,
    ImageItem,
    Item,
    ItemType,
    MessageItem,
    MessageRole,
    SpeechResultItem,
    SpeechSegmentItem,
    SpeechSegmentKind,
    SpeechWord,
    TensorDataType,
    TensorItem,
    TextItem,
    TextItemType,
    ToolCallItem,
    ToolResultItem,
)
from foundry_local_sdk.logging_helper import LogLevel
from foundry_local_sdk.model_http_transport import (
    HttpHeader,
    HttpRequest,
    HttpResponseHead,
    ModelHttpOperation,
    ModelHttpTransport,
)
from foundry_local_sdk.model_info import (
    DeviceType,
    ModelInfo,
    ModelInfoBuilder,
    ModelSettings,
    Parameter,
    PromptTemplate,
    Runtime,
)
from foundry_local_sdk.request import Request
from foundry_local_sdk.response import Response
from foundry_local_sdk.session import (
    AudioSession,
    ChatSession,
    DecisionSession,
    EmbeddingsSession,
    RankingSession,
    Session,
    StreamingResponse,
)
from foundry_local_sdk.session_types import (
    ChoiceDecisionAnswer,
    ChoiceDecisionQuestion,
    DecisionAnswer,
    DecisionQuestion,
    DecisionQuestionType,
    DecisionRequest,
    DecisionResult,
    DecisionUsage,
    FinishReason,
    JSONValue,
    NoulDecisionAnswer,
    NoulDecisionQuestion,
    RankedCandidate,
    RankingRequest,
    RankingResult,
    RequestOptions,
    RequestPreflightResult,
    ScoreDecisionAnswer,
    ScoreDecisionQuestion,
    SearchOptions,
    TokenUsage,
    ToolChoice,
)
from foundry_local_sdk.version import __version__

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.WARNING)
_sc = logging.StreamHandler(stream=sys.stdout)
_formatter = logging.Formatter(
    "[foundry-local] | %(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_sc.setFormatter(_formatter)
_logger.addHandler(_sc)
_logger.propagate = False

# Alias for callers who prefer the un-prefixed name.
Model = IModel

__all__ = [
    "AudioItem",
    "AudioSession",
    "BytesItem",
    "Catalog",
    "CatalogType",
    "ChatSession",
    "ChoiceDecisionAnswer",
    "ChoiceDecisionQuestion",
    "Configuration",
    "DecisionAnswer",
    "DecisionQuestion",
    "DecisionQuestionType",
    "DecisionRequest",
    "DecisionResult",
    "DecisionSession",
    "DecisionUsage",
    "DeviceType",
    "EmbeddingsSession",
    "EpDownloadResult",
    "EpInfo",
    "FinishReason",
    "FoundryLocalException",
    "FoundryLocalManager",
    "HttpHeader",
    "HttpRequest",
    "HttpResponseHead",
    "IModel",
    "ImageItem",
    "Item",
    "ItemQueue",
    "ItemType",
    "JSONValue",
    "LogLevel",
    "MessageItem",
    "MessageRole",
    "Model",
    "ModelHttpOperation",
    "ModelHttpTransport",
    "ModelInfo",
    "ModelInfoBuilder",
    "ModelSettings",
    "NoulDecisionAnswer",
    "NoulDecisionQuestion",
    "Parameter",
    "PromptTemplate",
    "RankedCandidate",
    "RankingRequest",
    "RankingResult",
    "RankingSession",
    "Request",
    "RequestOptions",
    "RequestPreflightResult",
    "Response",
    "Runtime",
    "ScoreDecisionAnswer",
    "ScoreDecisionQuestion",
    "SearchOptions",
    "Session",
    "SpeechResultItem",
    "SpeechSegmentItem",
    "SpeechSegmentKind",
    "SpeechWord",
    "StreamingResponse",
    "TensorDataType",
    "TensorItem",
    "TextItem",
    "TextItemType",
    "TokenUsage",
    "ToolCallItem",
    "ToolChoice",
    "ToolResultItem",
    "__version__",
]

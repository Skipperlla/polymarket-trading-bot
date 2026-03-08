"""
PolyRelayerClient - Full working wrapper for Polymarket Builder Relayer Client.

Handles on-chain operations:
  - Merge outcome tokens back into USDC collateral
  - Redeem winning positions after market resolution
  - Execute generic relayer transactions
  - ABI encoding for CTF (Conditional Token Framework) calls
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PolyRelayerClient")

try:
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address

    ETH_UTILS_AVAILABLE = True
except ImportError:
    ETH_UTILS_AVAILABLE = False
    logger.warning("eth_abi / eth_utils not installed. ABI encoding unavailable.")

try:
    from web3 import Web3
    from web3.constants import HASH_ZERO

    WEB3_AVAILABLE = True
except ImportError:
    Web3 = None  # type: ignore
    HASH_ZERO = "0x" + "00" * 32
    WEB3_AVAILABLE = False

try:
    from py_builder_relayer_client.client import (
        RelayClient,  # pyright: ignore[reportMissingImports]
    )
    from py_builder_relayer_client.models import OperationType, SafeTransaction
    from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

    RELAYER_AVAILABLE = True
except ImportError:
    RelayClient = None  # type: ignore
    BuilderConfig = None  # type: ignore
    BuilderApiKeyCreds = None  # type: ignore
    SafeTransaction = None  # type: ignore
    OperationType = None  # type: ignore
    RELAYER_AVAILABLE = False


class PolyRelayerClient:
    """
    Client wrapper for Polymarket Builder Relayer.

    Provides on-chain merge/redeem operations through the Polymarket
    relayer infrastructure (Gnosis Safe based).
    """

    # Polymarket contract addresses (Polygon mainnet)
    CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
    NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    POLYMARKET_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

    def __init__(
        self,
        relayer_url: str,
        chain_id: int,
        private_key: str,
        builder_api_key: Optional[str] = None,
        builder_secret: Optional[str] = None,
        builder_passphrase: Optional[str] = None,
    ):
        """
        Initialise PolyRelayerClient.

        Args:
            relayer_url: Relayer service URL.
            chain_id: Blockchain chain ID (137 for Polygon mainnet).
            private_key: Private key for signing transactions.
            builder_api_key: Builder API key (falls back to BUILDER_API_KEY env).
            builder_secret: Builder API secret (falls back to BUILDER_SECRET env).
            builder_passphrase: Builder API passphrase (falls back to BUILDER_PASS_PHRASE env).
        """
        self.relayer_url = relayer_url
        self.chain_id = chain_id
        self.private_key = private_key
        self.builder_api_key = builder_api_key or os.getenv("BUILDER_API_KEY", "")
        self.builder_secret = builder_secret or os.getenv("BUILDER_SECRET", "")
        self.builder_passphrase = builder_passphrase or os.getenv(
            "BUILDER_PASS_PHRASE", ""
        )

        self.builder_config: Optional[Any] = None
        self.client: Optional[Any] = None

        if not RELAYER_AVAILABLE:
            logger.warning(
                "py-builder-relayer-client not installed. "
                "Run: pip install py-builder-relayer-client py-builder-signing-sdk"
            )
            return

        if not (
            self.builder_api_key and self.builder_secret and self.builder_passphrase
        ):
            logger.warning(
                "Builder API credentials incomplete – relayer client not initialised. "
                "Set BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE."
            )
            return

        try:
            self.builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=self.builder_api_key,
                    secret=self.builder_secret,
                    passphrase=self.builder_passphrase,
                )
            )
            self.client = RelayClient(
                self.relayer_url,
                self.chain_id,
                self.private_key,
                self.builder_config,
            )
            logger.info(
                "PolyRelayerClient initialised (url=%s, chain=%s)",
                relayer_url,
                chain_id,
            )
        except Exception as exc:
            logger.exception("Failed to initialise relayer client: %s", exc)
            self.builder_config = None
            self.client = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True when the relayer client is ready."""
        return self.client is not None

    # ------------------------------------------------------------------
    # ABI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def function_selector(signature: str) -> bytes:
        """
        Compute the 4-byte function selector for a Solidity function signature.

        Example:
            function_selector("mergePositions(address,bytes32,bytes32,uint256[],uint256)")
            → first 4 bytes of keccak256(signature)
        """
        if not ETH_UTILS_AVAILABLE:
            raise RuntimeError("eth_utils is required for function_selector")
        return keccak(text=signature)[:4]

    @staticmethod
    def _condition_id_to_bytes32(condition_id: str) -> bytes:
        """
        Convert a hex condition_id (with or without 0x prefix) to 32 bytes.
        """
        raw = condition_id.replace("0x", "").replace("0X", "")
        if len(raw) > 64:
            raw = raw[:64]
        raw = raw.zfill(64)
        return bytes.fromhex(raw)

    @staticmethod
    def _parent_collection_id_bytes(
        parent_collection_id: Optional[bytes] = None,
    ) -> bytes:
        """Return parent_collection_id or 32 zero bytes (default)."""
        if parent_collection_id is not None:
            return parent_collection_id
        return b"\x00" * 32

    # ------------------------------------------------------------------
    # Calldata encoding
    # ------------------------------------------------------------------

    def encode_merge_positions_data(
        self,
        condition_id: str,
        partition: List[int],
        amount: int,
        collateral_token: Optional[str] = None,
        parent_collection_id: Optional[bytes] = None,
    ) -> str:
        """
        Encode calldata for CTF.mergePositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] partition,
            uint256 amount
        )
        """
        if not ETH_UTILS_AVAILABLE:
            raise RuntimeError("eth_abi/eth_utils required for ABI encoding")

        collateral = collateral_token or self.USDC_ADDRESS
        parent = self._parent_collection_id_bytes(parent_collection_id)
        cond_bytes = self._condition_id_to_bytes32(condition_id)

        selector = self.function_selector(
            "mergePositions(address,bytes32,bytes32,uint256[],uint256)"
        )
        encoded_args = encode(
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [
                to_checksum_address(collateral),
                parent,
                cond_bytes,
                partition,
                amount,
            ],
        )
        return "0x" + selector.hex() + encoded_args.hex()

    def encode_redeem_positions_data(
        self,
        condition_id: str,
        index_sets: List[int],
        collateral_token: Optional[str] = None,
        parent_collection_id: Optional[bytes] = None,
    ) -> str:
        """
        Encode calldata for CTF.redeemPositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] indexSets
        )
        """
        if not ETH_UTILS_AVAILABLE:
            raise RuntimeError("eth_abi/eth_utils required for ABI encoding")

        collateral = collateral_token or self.USDC_ADDRESS
        parent = self._parent_collection_id_bytes(parent_collection_id)
        cond_bytes = self._condition_id_to_bytes32(condition_id)

        selector = self.function_selector(
            "redeemPositions(address,bytes32,bytes32,uint256[])"
        )
        encoded_args = encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [
                to_checksum_address(collateral),
                parent,
                cond_bytes,
                index_sets,
            ],
        )
        return "0x" + selector.hex() + encoded_args.hex()

    # ------------------------------------------------------------------
    # On-chain operations
    # ------------------------------------------------------------------

    def merge_tokens(
        self,
        condition_id: str,
        amount: int,
        collateral_token: Optional[str] = None,
        parent_collection_id: Optional[bytes] = None,
        partition: Optional[List[int]] = None,
    ) -> Optional[Any]:
        """
        Merge outcome tokens back into collateral (USDC).

        Merging 1 "Yes" + 1 "No" token → 1 USDC (minus fees if any).

        Args:
            condition_id: Market condition ID (hex, 0x-prefixed or raw).
            amount: Amount in token units (6 decimals for USDC, so 1 USDC = 1_000_000).
            collateral_token: Collateral address (default: USDC on Polygon).
            parent_collection_id: Parent collection (default: bytes32(0)).
            partition: Index sets (default: [1, 2] for binary Yes/No markets).

        Returns:
            Relayer transaction response or None on failure.
        """
        if not self.client:
            logger.error("Relayer client not initialised – cannot merge tokens.")
            return None

        if partition is None:
            partition = [1, 2]

        try:
            calldata = self.encode_merge_positions_data(
                condition_id=condition_id,
                partition=partition,
                amount=amount,
                collateral_token=collateral_token,
                parent_collection_id=parent_collection_id,
            )

            tx = self._build_safe_transaction(
                to=self.CTF_ADDRESS,
                data=calldata,
                value="0",
            )

            resp = self.client.send_transaction(tx)
            logger.info(
                "merge_tokens sent – condition=%s amount=%d → %s",
                condition_id[:16],
                amount,
                resp,
            )
            return resp

        except Exception as exc:
            logger.exception("merge_tokens failed: %s", exc)
            return None

    def redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[List[int]] = None,
        collateral_token: Optional[str] = None,
        parent_collection_id: Optional[bytes] = None,
    ) -> Optional[Any]:
        """
        Redeem winning outcome tokens for collateral after market resolution.

        Args:
            condition_id: Market condition ID.
            index_sets: Which outcome slots to redeem (default: [1, 2] for binary).
            collateral_token: Collateral address (default: USDC).
            parent_collection_id: Parent collection (default: bytes32(0)).

        Returns:
            Relayer transaction response or None on failure.
        """
        if not self.client:
            logger.error("Relayer client not initialised – cannot redeem positions.")
            return None

        if index_sets is None:
            index_sets = [1, 2]

        try:
            calldata = self.encode_redeem_positions_data(
                condition_id=condition_id,
                index_sets=index_sets,
                collateral_token=collateral_token,
                parent_collection_id=parent_collection_id,
            )

            tx = self._build_safe_transaction(
                to=self.CTF_ADDRESS,
                data=calldata,
                value="0",
            )

            resp = self.client.send_transaction(tx)
            logger.info(
                "redeem_positions sent – condition=%s index_sets=%s → %s",
                condition_id[:16],
                index_sets,
                resp,
            )
            return resp

        except Exception as exc:
            logger.exception("redeem_positions failed: %s", exc)
            return None

    def execute_transaction(
        self,
        to: str,
        data: str,
        value: str = "0",
        description: str = "Execute transaction",
    ) -> Optional[Any]:
        """
        Execute a generic transaction through the relayer.

        Args:
            to: Target contract address.
            data: Hex-encoded calldata.
            value: ETH/MATIC value in wei (usually "0").
            description: Human-readable description for logging.

        Returns:
            Relayer transaction response or None on failure.
        """
        if not self.client:
            logger.error("Relayer client not initialised – cannot execute transaction.")
            return None

        try:
            tx = self._build_safe_transaction(to=to, data=data, value=value)
            resp = self.client.send_transaction(tx)
            logger.info("%s → %s", description, resp)
            return resp
        except Exception as exc:
            logger.exception("execute_transaction failed (%s): %s", description, exc)
            return None

    def execute_batch(
        self,
        transactions: List[Dict[str, str]],
        description: str = "Execute batch",
    ) -> Optional[Any]:
        """
        Execute multiple transactions via the relayer.

        Args:
            transactions: List of dicts with keys 'to', 'data', and optionally 'value'.
            description: Human-readable description for logging.

        Returns:
            Relayer transaction response or None on failure.
        """
        if not self.client:
            logger.error("Relayer client not initialised.")
            return None

        results = []
        for i, tx_dict in enumerate(transactions):
            try:
                tx = self._build_safe_transaction(
                    to=tx_dict["to"],
                    data=tx_dict.get("data", "0x"),
                    value=tx_dict.get("value", "0"),
                )
                resp = self.client.send_transaction(tx)
                logger.info(
                    "%s [%d/%d] → %s", description, i + 1, len(transactions), resp
                )
                results.append(resp)
            except Exception as exc:
                logger.exception(
                    "%s [%d/%d] failed: %s", description, i + 1, len(transactions), exc
                )
                results.append(None)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_safe_transaction(
        self,
        to: str,
        data: str,
        value: str = "0",
    ) -> Any:
        """
        Build a SafeTransaction object for the relayer.
        """
        if SafeTransaction is None or OperationType is None:
            raise RuntimeError(
                "py-builder-relayer-client not available – cannot build SafeTransaction"
            )

        if ETH_UTILS_AVAILABLE:
            to = to_checksum_address(to)

        return SafeTransaction(
            to=to,
            value=value,
            data=data,
            operation=OperationType.CALL,
        )

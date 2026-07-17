#!/usr/bin/env python3
"""Chain config loader for scanner prototypes.

The loader keeps chain metadata and secret locations separate. It returns env var
names and optionally resolved values, but config files should never contain live
RPC/API secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import os

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import check covers dependency
    raise RuntimeError("PyYAML is required to load chains.yaml") from exc

DEFAULT_CHAINS_PATH = Path(__file__).with_name("chains.yaml")


@dataclass(frozen=True)
class ChainConfig:
    key: str
    name: str
    chain_id: int
    dexscreener_chain: str
    native_symbol: str
    rpc_env: str = ""
    alchemy_api_key_env: str = ""
    alchemy_network: str = ""
    explorer_url: str = ""
    blockscout_url: str = ""
    default_pair_address: str = ""
    default_token_address: str = ""
    search_queries: tuple[str, ...] = ()
    filters: Dict[str, Any] | None = None

    @property
    def rpc_url(self) -> Optional[str]:
        return os.environ.get(self.rpc_env) if self.rpc_env else None

    @property
    def alchemy_api_key(self) -> Optional[str]:
        return os.environ.get(self.alchemy_api_key_env) if self.alchemy_api_key_env else None

    def as_public_dict(self) -> Dict[str, Any]:
        """Return non-secret config values for logs/API output."""
        return {
            "key": self.key,
            "name": self.name,
            "chain_id": self.chain_id,
            "dexscreener_chain": self.dexscreener_chain,
            "native_symbol": self.native_symbol,
            "rpc_env": self.rpc_env,
            "alchemy_api_key_env": self.alchemy_api_key_env,
            "alchemy_network": self.alchemy_network,
            "explorer_url": self.explorer_url,
            "blockscout_url": self.blockscout_url,
            "default_pair_address": self.default_pair_address,
            "default_token_address": self.default_token_address,
            "search_queries": list(self.search_queries),
            "filters": dict(self.filters or {}),
            "rpc_configured": bool(self.rpc_url),
            "alchemy_configured": bool(self.alchemy_api_key),
        }


def load_chain_configs(path: str | Path = DEFAULT_CHAINS_PATH) -> Dict[str, ChainConfig]:
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    chains = raw.get("chains") or {}
    loaded: Dict[str, ChainConfig] = {}
    for key, data in chains.items():
        loaded[key] = ChainConfig(
            key=key,
            name=str(data.get("name", key)),
            chain_id=int(data.get("chain_id", 0)),
            dexscreener_chain=str(data.get("dexscreener_chain", key)),
            native_symbol=str(data.get("native_symbol", "ETH")),
            rpc_env=str(data.get("rpc_env", "")),
            alchemy_api_key_env=str(data.get("alchemy_api_key_env", "")),
            alchemy_network=str(data.get("alchemy_network", "")),
            explorer_url=str(data.get("explorer_url", "")),
            blockscout_url=str(data.get("blockscout_url", "")),
            default_pair_address=str(data.get("default_pair_address", "")),
            default_token_address=str(data.get("default_token_address", "")),
            search_queries=tuple(data.get("search_queries") or ()),
            filters=dict(data.get("filters") or {}),
        )
    return loaded


def get_chain_config(chain: str, path: str | Path = DEFAULT_CHAINS_PATH) -> ChainConfig:
    configs = load_chain_configs(path)
    key = chain.lower()
    if key not in configs:
        available = ", ".join(sorted(configs))
        raise KeyError(f"Unknown chain '{chain}'. Available chains: {available}")
    return configs[key]

"""Run-level configuration surface for the declared variant register (P1-01).

``variants.py`` owns the ``V01``–``V35`` specification and the single selection
read path, :meth:`ict_trader.variants.VariantConfig.read`.  This module binds
that register to a run configuration and exposes the one reproducibility hash
required by SYSTEM_SPEC §15.6 A2::

    (rule_ids, variant_ids, bar_time, config_hash)

The hash covers the entire declared variant set plus any additional declared
configuration fields a caller passes.  It is canonical (sorted keys, stable
separators) and therefore byte-identical for the same declarations, as A1
requires.  Nothing here chooses a variant: an empty register hashes as an empty
register and every read through it refuses with ``NT_VARIANT_UNDECLARED``.

A7: no module may hold a private copy of a variant.  Consumers hold a
:class:`~ict_trader.variants.VariantConfig` and call ``read``; this module does
not add a second read path.
"""
from __future__ import annotations

import hashlib
import json
from typing import Mapping

from .variants import (
    DEFAULT_POLICIES,
    SPEC_REFUSAL_CODES,
    VARIANT_REGISTER,
    DefaultPolicy,
    Refusal,
    RefusalCode,
    UnknownVariant,
    VariantConfig,
    VariantSpec,
    VariantUndeclared,
    read_variant_spec,
)

__all__ = [
    "CONFIG_HASH_SCHEMA",
    "DEFAULT_POLICIES",
    "SPEC_REFUSAL_CODES",
    "VARIANT_REGISTER",
    "DefaultPolicy",
    "Refusal",
    "RefusalCode",
    "UnknownVariant",
    "VariantConfig",
    "VariantSpec",
    "VariantUndeclared",
    "config_hash",
    "read_variant_spec",
]

#: Version tag mixed into the hash so a change to the hash's payload shape
#: invalidates old digests instead of silently colliding with them.
CONFIG_HASH_SCHEMA = "ict-variant-config/1"


def config_hash(
    register: VariantConfig | Mapping[str, str] | None = None,
    /,
    **values: object,
) -> str:
    """SHA-256 of the declared variants and any additional config fields.

    ``register`` may be a :class:`VariantConfig`, a plain ``{variant_id:
    option}`` mapping (validated by ``VariantConfig``), or ``None`` for the
    empty register.  ``values`` are extra declared configuration fields; they
    are stringified deterministically and key-sorted, never averaged or
    dropped.  The result is a 64-character lowercase hex digest.

    No default traversal happens here: an empty register is hashed as empty,
    and a missing fork remains a refusal rather than a hidden hash input.
    """
    if register is None:
        declared = VariantConfig()
    elif isinstance(register, VariantConfig):
        declared = register
    else:
        declared = VariantConfig(register)

    payload = {
        "schema": CONFIG_HASH_SCHEMA,
        "variants": declared.as_dict(),
        "config": {str(key): str(value) for key, value in sorted(values.items())},
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

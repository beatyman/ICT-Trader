"""Canonical chan.py configuration.

The source object is kept as JSON-shaped data so it can be compared with the
upstream YAML/JSON.  `flat_config()` translates the nested sections into the
flat keys expected by chan.py's CChanConfig.
"""
from __future__ import annotations

from typing import Any

CHAN_CONFIG_VERSION = "chan-config-2026.11"

CHAN_CONFIG: dict[str, Any] = {
    "bi_config": {
        "bi_algo": "Normal",
        "is_strict": False,
        "bi_fx_check": "Strict",
        "gap_as_kl": True,
        "bi_end_is_peak": True,
        "bi_allow_sub_peak": False,
    },
    "seg_config": {
        "seg_algo": "Chan",
        "left_seg_method": "Peak",
    },
    "zs_config": {
        "need_combine": True,
        "zs_combine_mode": "ZS",
        "one_bi_zs": False,
        "zs_algo": "Normal",
    },
    "bs_point_config": {
        "b_conf": {
            "divergence_rate": "inf", "min_zs_cnt": 1,
            "bsp1_only_multibi_zs": True, "max_bs2_rate": 0.9999,
            "macd_algo": "Peak", "bs1_peak": True,
            "target_types": ["T1", "T1P", "T2", "T2S", "T3A", "T3B"],
            "bsp2_follow_1": False, "bsp3_follow_1": False,
            "bsp3_peak": False, "bsp2s_follow_2": False,
            "max_bsp2s_lv": None, "strict_bsp3": False,
            "bsp3a_max_zs_cnt": 1,
        },
        "s_conf": {
            "divergence_rate": "inf", "min_zs_cnt": 1,
            "bsp1_only_multibi_zs": True, "max_bs2_rate": 0.9999,
            "macd_algo": "Peak", "bs1_peak": True,
            "target_types": ["T1", "T1P", "T2", "T2S", "T3A", "T3B"],
            "bsp2_follow_1": False, "bsp3_follow_1": False,
            "bsp3_peak": False, "bsp2s_follow_2": False,
            "max_bsp2s_lv": None, "strict_bsp3": False,
            "bsp3a_max_zs_cnt": 1,
        },
    },
    "seg_bs_point_config": {
        "b_conf": {
            "divergence_rate": "inf", "min_zs_cnt": 1,
            "bsp1_only_multibi_zs": True, "max_bs2_rate": 0.9999,
            "macd_algo": "Slope", "bs1_peak": True,
            "target_types": ["T1", "T1P", "T2", "T2S", "T3A", "T3B"],
            "bsp2_follow_1": False, "bsp3_follow_1": False,
            "bsp3_peak": False, "bsp2s_follow_2": False,
            "max_bsp2s_lv": None, "strict_bsp3": False,
            "bsp3a_max_zs_cnt": 1,
        },
        "s_conf": {
            "divergence_rate": "inf", "min_zs_cnt": 1,
            "bsp1_only_multibi_zs": True, "max_bs2_rate": 0.9999,
            "macd_algo": "Slope", "bs1_peak": True,
            "target_types": ["T1", "T1P", "T2", "T2S", "T3A", "T3B"],
            "bsp2_follow_1": False, "bsp3_follow_1": False,
            "bsp3_peak": False, "bsp2s_follow_2": False,
            "max_bsp2s_lv": None, "strict_bsp3": False,
            "bsp3a_max_zs_cnt": 1,
        },
    },
    "global_config": {
        "trigger_step": True,
        "skip_step": 0,
        "kl_data_check": False,
        "max_kl_misalign_cnt": 2,
        "max_kl_inconsistent_cnt": 5,
        "auto_skip_illegal_sub_lv": False,
        "print_warning": False,
        "print_err_time": False,
        "mean_metrics": [5, 10, 20, 60],
        "trend_metrics": [5, 10, 20, 60],
    },
    "indicators": {
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "boll": {"period": 20, "std_dev": 2.0},
        "rsi": {"period": 14},
        "kdj": {"n": 9},
    },
}

_TARGET_MAP = {
    "T1": "1", "T1P": "1p", "T2": "2", "T2S": "2s",
    "T3A": "3a", "T3B": "3b",
}


def _bs_type(targets: list[str]) -> str:
    return ",".join(_TARGET_MAP[t] for t in targets)


def flat_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Translate the nested config into CChanConfig's flat keys."""
    cfg = raw or CHAN_CONFIG
    bi = cfg["bi_config"]
    seg = cfg["seg_config"]
    zs = cfg["zs_config"]
    glob = cfg["global_config"]
    ind = cfg["indicators"]
    out: dict[str, Any] = {
        "bi_algo": str(bi["bi_algo"]).lower(),
        "bi_strict": bool(bi["is_strict"]),
        "bi_fx_check": str(bi["bi_fx_check"]).lower(),
        "gap_as_kl": bool(bi["gap_as_kl"]),
        "bi_end_is_peak": bool(bi["bi_end_is_peak"]),
        "bi_allow_sub_peak": bool(bi["bi_allow_sub_peak"]),
        "seg_algo": str(seg["seg_algo"]).lower(),
        "left_seg_method": str(seg["left_seg_method"]).lower(),
        "zs_combine": bool(zs["need_combine"]),
        "zs_combine_mode": str(zs["zs_combine_mode"]).lower(),
        "one_bi_zs": bool(zs["one_bi_zs"]),
        "zs_algo": str(zs["zs_algo"]).lower(),
        "trigger_step": bool(glob["trigger_step"]),
        "skip_step": int(glob["skip_step"]),
        "kl_data_check": bool(glob["kl_data_check"]),
        "max_kl_misalgin_cnt": int(glob["max_kl_misalign_cnt"]),
        "max_kl_inconsistent_cnt": int(glob["max_kl_inconsistent_cnt"]),
        "auto_skip_illegal_sub_lv": bool(glob["auto_skip_illegal_sub_lv"]),
        "print_warning": bool(glob["print_warning"]),
        "print_err_time": bool(glob["print_err_time"]),
        "mean_metrics": list(glob["mean_metrics"]),
        "trend_metrics": list(glob["trend_metrics"]),
        "macd": dict(ind["macd"]),
        "boll_n": int(ind["boll"]["period"]),
        "cal_rsi": True,
        "rsi_cycle": int(ind["rsi"]["period"]),
        "cal_kdj": True,
        "kdj_cycle": int(ind["kdj"]["n"]),
    }
    # The nested b/s and seg_b/s configs are expressed by chan.py as
    # KEY-buy / KEY-sell / KEY-segbuy / KEY-segsell flat entries.
    for section, suffix_b, suffix_s in (
        ("bs_point_config", "-buy", "-sell"),
        ("seg_bs_point_config", "-segbuy", "-segsell"),
    ):
        conf_b = cfg[section]["b_conf"]
        conf_s = cfg[section]["s_conf"]
        for key, value in conf_b.items():
            if key == "target_types":
                continue
            out[f"{key}{suffix_b}"] = str(value).lower() if key == "macd_algo" else value
        for key, value in conf_s.items():
            if key == "target_types":
                continue
            out[f"{key}{suffix_s}"] = str(value).lower() if key == "macd_algo" else value
        # chan.py's base bs_type is shared by the buy/sell parsers; the
        # supplied buy and sell target sets are identical.
        out["bs_type"] = _bs_type(conf_b["target_types"])
    for key in list(out):
        if key.startswith("divergence_rate"):
            out[key] = float("inf")
    return out


def describe() -> dict[str, Any]:
    return {"version": CHAN_CONFIG_VERSION, "config": CHAN_CONFIG}

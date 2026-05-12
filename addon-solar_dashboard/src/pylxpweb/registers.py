"""Register mapping constants for Luxpower/EG4 inverter devices.

This module contains the register addresses and block sizes discovered through
empirical testing. See docs/claude/REGISTER_MAPPING_ANALYSIS.md for details.

IMPORTANT: Multi-register parameters require reading the full block starting
from the original register, even if the data is in the last register(s).

Example:
    # WRONG - Will fail
    read_parameters(serial_num, start_register=134, point_number=1)

    # CORRECT - Use REGISTER_BLOCKS
    block = REGISTER_BLOCKS_18KPV[134]
    read_parameters(
        serial_num,
        start_register=block['read_start'],  # 126
        point_number=block['read_size']      # 9
    )
"""

from __future__ import annotations

from typing import TypedDict


class RegisterBlock(TypedDict):
    """Register block definition.

    Attributes:
        register: Actual register position where data is located
        size: Number of registers containing data
        read_start: Starting register for API read (may include leading empty registers)
        read_size: Block size for API read (may be larger than size)
        parameters: List of parameter names available in this block
    """

    register: int
    size: int
    read_start: int
    read_size: int
    parameters: list[str]


# 18KPV Register Blocks
# Total blocks: 26
# Register range: 0-268
# Unique parameters: 51
REGISTER_BLOCKS_18KPV: dict[int, RegisterBlock] = {
    0: {
        "register": 0,
        "size": 2,
        "read_start": 0,
        "read_size": 2,
        "parameters": [
            "HOLD_MODEL",
            "HOLD_MODEL_batteryType",
            "HOLD_MODEL_leadAcidType",
            "HOLD_MODEL_lithiumType",
            "HOLD_MODEL_measurement",
            "HOLD_MODEL_meterBrand",
            "HOLD_MODEL_meterType",
            "HOLD_MODEL_powerRating",
            "HOLD_MODEL_rule",
            "HOLD_MODEL_ruleMask",
            "HOLD_MODEL_usVersion",
            "HOLD_MODEL_wirelessMeter",
            "INPUT_BATTERY_VOLTAGE",
        ],
    },
    2: {
        "register": 2,
        "size": 5,
        "read_start": 2,
        "read_size": 5,
        "parameters": ["HOLD_SERIAL_NUM"],
    },
    7: {
        "register": 7,
        "size": 4,
        "read_start": 7,
        "read_size": 4,
        "parameters": ["HOLD_FW_CODE"],
    },
    12: {
        "register": 12,
        "size": 3,
        "read_start": 11,
        "read_size": 4,
        "parameters": ["HOLD_TIME"],
    },
    19: {
        "register": 19,
        "size": 1,
        "read_start": 17,
        "read_size": 3,
        "parameters": ["BIT_DEVICE_TYPE_ODM", "BIT_MACHINE_TYPE", "HOLD_DEVICE_TYPE_CODE"],
    },
    96: {
        "register": 96,
        "size": 1,
        "read_start": 92,
        "read_size": 5,
        "parameters": ["HOLD_DELAY_TIME_FOR_QV_CURVE"],
    },
    99: {
        "register": 99,
        "size": 1,
        "read_start": 98,
        "read_size": 2,
        "parameters": ["HOLD_LEAD_ACID_CHARGE_VOLT_REF"],
    },
    105: {
        "register": 105,
        "size": 1,
        "read_start": 104,
        "read_size": 2,
        "parameters": ["HOLD_DISCHG_CUT_OFF_SOC_EOD"],
    },
    112: {
        "register": 112,
        "size": 1,
        "read_start": 111,
        "read_size": 2,
        "parameters": ["HOLD_SET_MASTER_OR_SLAVE"],
    },
    115: {
        "register": 115,
        "size": 1,
        "read_start": 114,
        "read_size": 2,
        "parameters": ["_12K_HOLD_OVF_DERATE_START_POINT"],
    },
    118: {
        "register": 118,
        "size": 1,
        "read_start": 117,
        "read_size": 2,
        "parameters": ["HOLD_VBAT_START_DERATING"],
    },
    125: {
        "register": 125,
        "size": 1,
        "read_start": 123,
        "read_size": 3,
        "parameters": ["HOLD_SOC_LOW_LIMIT_EPS_DISCHG"],
    },
    134: {
        "register": 134,
        "size": 1,
        "read_start": 126,
        "read_size": 9,
        "parameters": ["HOLD_UVF_DERATE_START_POINT"],
    },
    136: {
        "register": 136,
        "size": 1,
        "read_start": 135,
        "read_size": 2,
        "parameters": ["HOLD_OVF_DROOP_KOF"],
    },
    144: {
        "register": 144,
        "size": 1,
        "read_start": 138,
        "read_size": 7,
        "parameters": ["HOLD_FLOATING_VOLTAGE"],
    },
    148: {
        "register": 148,
        "size": 1,
        "read_start": 147,
        "read_size": 2,
        "parameters": ["HOLD_NOMINAL_BATTERY_VOLTAGE"],
    },
    176: {
        "register": 176,
        "size": 1,
        "read_start": 170,
        "read_size": 7,
        "parameters": ["HOLD_MAX_AC_INPUT_POWER"],
    },
    181: {
        "register": 181,
        "size": 1,
        "read_start": 180,
        "read_size": 2,
        "parameters": ["HOLD_VOLT_WATT_V1"],
    },
    193: {
        "register": 193,
        "size": 1,
        "read_start": 192,
        "read_size": 2,
        "parameters": ["HOLD_UVF_DROOP_KUF"],
    },
    201: {
        "register": 201,
        "size": 1,
        "read_start": 199,
        "read_size": 3,
        "parameters": ["_12K_HOLD_CHARGE_FIRST_VOLT"],
    },
    226: {
        "register": 226,
        "size": 1,
        "read_start": 225,
        "read_size": 2,
        "parameters": [
            "BIT_AC_COUPLE_ON_EPS_PORT",
            "FUNC_AC_COUPLE_DARK_START_EN",
            "FUNC_AC_COUPLE_ON_EPS_PORT_EN",
            "FUNC_GEN_CTRL",
            "FUNC_GRID_CT_CONNECTION_EN",
            "FUNC_MIDBOX_EN",
            "FUNC_N_PE_CONNECT_INNER_EN",
            "FUNC_PARALLEL_DATA_SYNC_EN",
            "FUNC_RETAIN_SHUTDOWN",
            "FUNC_RETAIN_STANDBY",
            "FUNC_RUN_WITHOUT_GRID_12K",
        ],
    },
    232: {
        "register": 232,
        "size": 1,
        "read_start": 231,
        "read_size": 2,
        "parameters": ["_12K_HOLD_GRID_PEAK_SHAVING_POWER_2"],
    },
    237: {
        "register": 237,
        "size": 1,
        "read_start": 234,
        "read_size": 4,
        "parameters": ["_12K_HOLD_GEN_COOL_DOWN_TIME"],
    },
    244: {
        "register": 244,
        "size": 1,
        "read_start": 241,
        "read_size": 4,
        "parameters": ["_12K_HOLD_BOOT_LOADER_VERSION"],
    },
    256: {
        "register": 256,
        "size": 1,
        "read_start": 255,
        "read_size": 2,
        "parameters": ["HOLD_GEN_START_HOUR_1", "HOLD_GEN_START_MINUTE_1"],
    },
    268: {
        "register": 268,
        "size": 1,
        "read_start": 261,
        "read_size": 8,
        "parameters": ["HOLD_EXPORT_LOCK_POWER"],
    },
}


# GridBOSS Register Blocks
# Total blocks: 22
# Register range: 0-2101
# Unique parameters: 54
REGISTER_BLOCKS_GRIDBOSS: dict[int, RegisterBlock] = {
    0: {
        "register": 0,
        "size": 2,
        "read_start": 0,
        "read_size": 2,
        "parameters": [
            "HOLD_MODEL",
            "HOLD_MODEL_batteryType",
            "HOLD_MODEL_leadAcidType",
            "HOLD_MODEL_lithiumType",
            "HOLD_MODEL_measurement",
            "HOLD_MODEL_meterBrand",
            "HOLD_MODEL_meterType",
            "HOLD_MODEL_powerRating",
            "HOLD_MODEL_rule",
            "HOLD_MODEL_ruleMask",
            "HOLD_MODEL_usVersion",
            "HOLD_MODEL_wirelessMeter",
        ],
    },
    2: {
        "register": 2,
        "size": 5,
        "read_start": 2,
        "read_size": 5,
        "parameters": ["HOLD_SERIAL_NUM"],
    },
    7: {
        "register": 7,
        "size": 4,
        "read_start": 7,
        "read_size": 4,
        "parameters": ["HOLD_FW_CODE"],
    },
    12: {
        "register": 12,
        "size": 3,
        "read_start": 11,
        "read_size": 4,
        "parameters": ["HOLD_TIME"],
    },
    19: {
        "register": 19,
        "size": 1,
        "read_start": 17,
        "read_size": 3,
        "parameters": ["BIT_DEVICE_TYPE_ODM", "BIT_MACHINE_TYPE", "HOLD_DEVICE_TYPE_CODE"],
    },
    99: {
        "register": 99,
        "size": 1,
        "read_start": 92,
        "read_size": 8,
        "parameters": ["HOLD_LEAD_ACID_CHARGE_VOLT_REF"],
    },
    105: {
        "register": 105,
        "size": 1,
        "read_start": 104,
        "read_size": 2,
        "parameters": ["HOLD_DISCHG_CUT_OFF_SOC_EOD"],
    },
    112: {
        "register": 112,
        "size": 1,
        "read_start": 111,
        "read_size": 2,
        "parameters": ["HOLD_SET_MASTER_OR_SLAVE"],
    },
    116: {
        "register": 116,
        "size": 1,
        "read_start": 114,
        "read_size": 3,
        "parameters": ["HOLD_P_TO_USER_START_DISCHG"],
    },
    118: {
        "register": 118,
        "size": 1,
        "read_start": 117,
        "read_size": 2,
        "parameters": ["HOLD_VBAT_START_DERATING"],
    },
    122: {
        "register": 122,
        "size": 1,
        "read_start": 121,
        "read_size": 2,
        "parameters": ["HOLD_MAINTENANCE_COUNT"],
    },
    125: {
        "register": 125,
        "size": 1,
        "read_start": 123,
        "read_size": 3,
        "parameters": ["HOLD_SOC_LOW_LIMIT_EPS_DISCHG"],
    },
    137: {
        "register": 137,
        "size": 1,
        "read_start": 126,
        "read_size": 12,
        "parameters": ["HOLD_SPEC_LOAD_COMPENSATE"],
    },
    144: {
        "register": 144,
        "size": 1,
        "read_start": 138,
        "read_size": 7,
        "parameters": ["HOLD_FLOATING_VOLTAGE"],
    },
    148: {
        "register": 148,
        "size": 1,
        "read_start": 147,
        "read_size": 2,
        "parameters": ["HOLD_NOMINAL_BATTERY_VOLTAGE"],
    },
    176: {
        "register": 176,
        "size": 1,
        "read_start": 170,
        "read_size": 7,
        "parameters": ["HOLD_MAX_AC_INPUT_POWER"],
    },
    194: {
        "register": 194,
        "size": 1,
        "read_start": 180,
        "read_size": 15,
        "parameters": ["OFF_GRID_HOLD_GEN_CHG_START_VOLT"],
    },
    204: {
        "register": 204,
        "size": 1,
        "read_start": 199,
        "read_size": 6,
        "parameters": ["_12K_HOLD_LEAD_CAPACITY"],
    },
    215: {
        "register": 215,
        "size": 1,
        "read_start": 205,
        "read_size": 11,
        "parameters": [
            "FUNC_LSP_OUTPUT_10_EN",
            "FUNC_LSP_OUTPUT_11_EN",
            "FUNC_LSP_OUTPUT_12_EN",
            "FUNC_LSP_OUTPUT_1_EN",
            "FUNC_LSP_OUTPUT_2_EN",
            "FUNC_LSP_OUTPUT_3_EN",
            "FUNC_LSP_OUTPUT_4_EN",
            "FUNC_LSP_OUTPUT_5_EN",
            "FUNC_LSP_OUTPUT_6_EN",
            "FUNC_LSP_OUTPUT_7_EN",
            "FUNC_LSP_OUTPUT_8_EN",
            "FUNC_LSP_OUTPUT_9_EN",
        ],
    },
    224: {
        "register": 224,
        "size": 1,
        "read_start": 222,
        "read_size": 3,
        "parameters": ["MIDBOX_HOLD_GEN_VOLT_SOC_ENABLE"],
    },
    2099: {
        "register": 2099,
        "size": 1,
        "read_start": 2033,
        "read_size": 67,
        "parameters": ["MIDBOX_HOLD_BUSBAR_PCS_RATING"],
    },
    2101: {
        "register": 2101,
        "size": 1,
        "read_start": 2100,
        "read_size": 2,
        "parameters": [
            "BIT_MID_INSTALL_POSITION",
            "BIT_SMART_LOAD_BASE_ON_1",
            "BIT_SMART_LOAD_BASE_ON_2",
            "BIT_SMART_LOAD_BASE_ON_3",
            "BIT_SMART_LOAD_BASE_ON_4",
            "BIT_SMART_LOAD_BASE_ON_TIME_SOC_VOLT_1",
            "BIT_SMART_LOAD_BASE_ON_TIME_SOC_VOLT_2",
            "BIT_SMART_LOAD_BASE_ON_TIME_SOC_VOLT_3",
            "BIT_SMART_LOAD_BASE_ON_TIME_SOC_VOLT_4",
        ],
    },
}


# Known register ranges from web UI
KNOWN_RANGES_18KPV = [
    (0, 127),
    (127, 127),
    (240, 127),
    # (269, 6),  # Parallel mode only
]

KNOWN_RANGES_GRIDBOSS = [
    (0, 127),
    (127, 127),
    (254, 127),
    (2032, 127),
]


def get_register_block(device_type: str, register: int) -> RegisterBlock | None:
    """Get register block definition for a specific register.

    Args:
        device_type: Device type ("18KPV" or "GridBOSS")
        register: Register number

    Returns:
        RegisterBlock if found, None otherwise
    """
    if device_type == "18KPV":
        return REGISTER_BLOCKS_18KPV.get(register)
    elif device_type == "GridBOSS":
        return REGISTER_BLOCKS_GRIDBOSS.get(register)
    return None


def get_all_parameters(device_type: str) -> set[str]:
    """Get all unique parameter names for a device type.

    Args:
        device_type: Device type ("18KPV" or "GridBOSS")

    Returns:
        Set of parameter names
    """
    if device_type == "18KPV":
        blocks = REGISTER_BLOCKS_18KPV.values()
    elif device_type == "GridBOSS":
        blocks = REGISTER_BLOCKS_GRIDBOSS.values()
    else:
        return set()

    params = set()
    for block in blocks:
        params.update(block["parameters"])
    return params

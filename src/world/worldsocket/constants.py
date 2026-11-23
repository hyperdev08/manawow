"""Protocol constants and pure formulas shared by the world socket mixins."""
from __future__ import annotations

import struct

from src.protocol.opcodes import Opcodes


__all__ = [
    "AUTH_OK",
    "AUTH_FAILED",
    "AUTH_REJECT",
    "AUTH_VERSION_MISMATCH",
    "AUTH_UNKNOWN_ACCOUNT",
    "CHAR_CREATE_SUCCESS",
    "CHAR_CREATE_FAILED",
    "CHAR_CREATE_NAME_IN_USE",
    "CHAR_DELETE_SUCCESS",
    "CHAR_DELETE_FAILED",
    "CHAR_LOGIN_NO_CHARACTER",
    "CLIENT_BUILD",
    "SPELL_FAILED_AFFECTING_COMBAT",
    "SPELL_FAILED_ALREADY_AT_FULL_HEALTH",
    "SPELL_FAILED_ALREADY_AT_FULL_POWER",
    "SPELL_FAILED_BAD_TARGETS",
    "SPELL_FAILED_CASTER_AURASTATE",
    "SPELL_FAILED_CASTER_DEAD",
    "SPELL_FAILED_INTERRUPTED",
    "SPELL_FAILED_LINE_OF_SIGHT",
    "SPELL_FAILED_NOT_BEHIND",
    "SPELL_FAILED_NOT_INFRONT",
    "SPELL_FAILED_NOT_KNOWN",
    "SPELL_FAILED_NOT_READY",
    "SPELL_FAILED_NOT_WHILE_GHOST",
    "SPELL_FAILED_NO_COMBO_POINTS",
    "SPELL_FAILED_NO_POWER",
    "SPELL_FAILED_OUT_OF_RANGE",
    "SPELL_FAILED_SPELL_IN_PROGRESS",
    "SPELL_FAILED_SPELL_UNAVAILABLE",
    "SPELL_FAILED_TARGETS_DEAD",
    "COMBAT_TICK_INTERVAL",
    "CREATURE_LEASH_RADIUS",
    "RESPAWN_SCAN_INTERVAL",
    "CREATURE_SPELL_LIST_INTERVAL",
    "CREATURE_SPELL_GCD",
    "SPELL_LIST_FLAG_SUPPORT_ACTION",
    "SPELL_LIST_FLAG_RANGED_ACTION",
    "SPELL_LIST_FLAG_NON_BLOCKING",
    "SPELL_LIST_FLAG_CATEGORY_COOLDOWN",
    "POSITION_SAVE_INTERVAL",
    "calculate_rage_conversion",
    "attacker_rage_gain_tenths",
    "victim_rage_gain_tenths",
    "CREATURE_VISIBILITY_RADIUS",
    "CREATURE_RESCAN_DISTANCE",
    "IDLE_WANDER_DELAY_MIN",
    "IDLE_WANDER_DELAY_MAX",
    "IDLE_WANDER_MAX_MOVES",
    "IDLE_WANDER_DEFAULT_RADIUS",
    "IDLE_WANDER_RETRY",
    "IDLE_MOVE_MIN_DURATION_MS",
    "IDLE_MOVE_MAX_DURATION_MS",
    "UNIT_FLAG_DISABLE_MOVE",
    "SUMMON_GUID_BASE",
    "SUMMON_GUID_MAX",
    "SUMMON_FOLLOW_DISTANCE",
    "SUMMON_FOLLOW_STOP_DISTANCE",
    "SUMMON_LEASH_DISTANCE",
    "SUMMON_MOVE_MAX_MS",
    "SUMMON_PET_SWING_INTERVAL",
    "MOVEMENT_OPCODES",
    "MOVEMENT_OPCODES_WITHOUT_GUID",
    "MOVEMENT_OPCODES_TIME_ONLY",
    "MOVEMENT_OPCODES_QUIET",
    "TEXT_EMOTE_TO_EMOTE",
    "CHAT_EMOTE_TO_TEXT_ID",
    "STATE_EMOTE_IDS",
]


AUTH_OK = 12


AUTH_FAILED = 13


AUTH_REJECT = 14


AUTH_VERSION_MISMATCH = 20


AUTH_UNKNOWN_ACCOUNT = 21


CHAR_CREATE_SUCCESS = 47


CHAR_CREATE_FAILED = 49


CHAR_CREATE_NAME_IN_USE = 50


CHAR_DELETE_SUCCESS = 71


CHAR_DELETE_FAILED = 72


CHAR_LOGIN_NO_CHARACTER = 0x53


CLIENT_BUILD = 12340


# ``SpellCastResult`` values the combat code reports in SMSG_CAST_FAILED.  The
# numbering is the one the 3.3.5 client uses to pick its error string, so an
# arbitrary number here shows up as a generic (or missing) message.
SPELL_FAILED_AFFECTING_COMBAT = 1


SPELL_FAILED_ALREADY_AT_FULL_HEALTH = 2


SPELL_FAILED_ALREADY_AT_FULL_POWER = 4


SPELL_FAILED_BAD_TARGETS = 12


SPELL_FAILED_CASTER_AURASTATE = 22


SPELL_FAILED_CASTER_DEAD = 23


SPELL_FAILED_INTERRUPTED = 40

SPELL_FAILED_LINE_OF_SIGHT = 47

SPELL_FAILED_NOT_BEHIND = 57

SPELL_FAILED_NOT_INFRONT = 61

SPELL_FAILED_NOT_KNOWN = 63


SPELL_FAILED_NOT_READY = 67


SPELL_FAILED_NOT_WHILE_GHOST = 73


SPELL_FAILED_NO_COMBO_POINTS = 78


SPELL_FAILED_NO_POWER = 85


SPELL_FAILED_OUT_OF_RANGE = 97


SPELL_FAILED_SPELL_IN_PROGRESS = 105


SPELL_FAILED_SPELL_UNAVAILABLE = 107


SPELL_FAILED_TARGETS_DEAD = 109


# Power regeneration period and the creature AI tick.  0.25 s is fine for both
# swing timers (>= 0.8 s) and chase splines, and cheap enough to run per session.
COMBAT_TICK_INTERVAL = 0.25


# A creature gives up and walks home after being pulled this far from its spawn.
CREATURE_LEASH_RADIUS = 30.0


# How often dead spawns are checked for respawn (seconds).
RESPAWN_SCAN_INTERVAL = 2.0


# ``creature_spell_list`` AI (CMaNGOS ``UnitAI::UpdateSpellLists``).  The list
# is evaluated every 1200 ms; the 1.5 s spell GCD then spaces actual casts out
# exactly like the reference server.
CREATURE_SPELL_LIST_INTERVAL = 1.2


CREATURE_SPELL_GCD = 1.5


# ``SpellListFlags`` (CMaNGOS ``CreatureSpellList.h``).
SPELL_LIST_FLAG_SUPPORT_ACTION = 0x01


SPELL_LIST_FLAG_RANGED_ACTION = 0x02


SPELL_LIST_FLAG_NON_BLOCKING = 0x04


SPELL_LIST_FLAG_CATEGORY_COOLDOWN = 0x08


# How often the client-reported position is written to SQLite while moving.
# The client sends MSG_MOVE_HEARTBEAT roughly twice a second, so this keeps
# the database writes cheap without losing more than a second of walking.
POSITION_SAVE_INTERVAL = 2.0


# Rage conversion and reward formulas, kept as pure functions so the protocol
# self-test can verify them without a live character.  They mirror CMaNGOS
# ``Player::CalculateRageConversion`` and ``Unit::CalculateMeleeDamage`` /
# ``Player::RewardRage`` exactly (3.3.5a build 12340).


def _f32(value: float) -> float:
    """Round ``value`` to IEEE-754 binary32, like a C++ ``float`` operation."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def calculate_rage_conversion(level: int) -> float:
    """``Player::CalculateRageConversion`` for ``level``."""
    level = max(1, int(level))
    inner = 0.0091107836 * level * level + 3.225598133 * level
    return _f32(_f32(inner) + _f32(4.2652911))


def attacker_rage_gain_tenths(damage: int, level: int,
                              weapon_speed: float, crit: bool = False) -> int:
    """Rage (in tenths) from one main-hand auto-attack.

    CMaNGOS truncates the weapon-speed term to an integer *before* halving the
    total: ``(damage / conversion * 7.5 + uint32(speed * 3.5 or 7)) / 2``.
    Every step is a C++ ``float`` operation, so the intermediate values are
    rounded to binary32 just like the server core.
    """
    conversion = calculate_rage_conversion(level)
    speed_factor = int(_f32(max(0.0, float(weapon_speed))
                            * (7.0 if crit else 3.5)))
    add_rage = _f32(damage / conversion)
    add_rage = _f32(add_rage * 7.5)
    add_rage = _f32(add_rage + speed_factor)
    add_rage = _f32(add_rage / 2.0)
    return int(_f32(add_rage * 10.0))


def victim_rage_gain_tenths(damage: int, level: int) -> int:
    """Rage (in tenths) from physical damage taken."""
    conversion = calculate_rage_conversion(level)
    add_rage = _f32(damage / conversion)
    add_rage = _f32(add_rage * 2.5)
    return int(_f32(add_rage * 10.0))


# Static-creature visibility.  The 3.3.5 client draws units at roughly this
# range; sending a bounded neighbourhood (instead of every spawn on the map,
# which is ~30k on Azeroth) keeps the login burst small and lets a full world
# database be loaded without a noticeable hitch.
CREATURE_VISIBILITY_RADIUS = 150.0


# Re-scan when the player has moved this far since the last scan.  Movement
# packets arrive twice a second; this avoids a spatial query for every step.
CREATURE_RESCAN_DISTANCE = 25.0


# Idle movement.  ``WanderMovementGenerator`` (CMaNGOS 3.3.5) pauses 3-10 s
# between groups of up to three consecutive random steps, walks at the
# creature's walk speed and picks each destination inside the spawn radius
# (``creature.spawndist``) around the respawn point.
IDLE_WANDER_DELAY_MIN = 3.0


IDLE_WANDER_DELAY_MAX = 10.0


IDLE_WANDER_MAX_MOVES = 3


IDLE_WANDER_DEFAULT_RADIUS = 5.0


# A failed path is retried quickly instead of waiting a full pause.
IDLE_WANDER_RETRY = 0.5


IDLE_MOVE_MIN_DURATION_MS = 200


IDLE_MOVE_MAX_DURATION_MS = 12000


# ``UNIT_FLAG_DISABLE_MOVE`` (3.3.5): rooted NPCs never wander.
UNIT_FLAG_DISABLE_MOVE = 0x00000004


# Summoned creatures (pets/guardians).  Runtime summons allocate spawn ids from
# the top of the 24-bit creature guid counter; the imported world dump stops far
# below 0xE00000, so a summon can never collide with a static spawn.
SUMMON_GUID_BASE = 0xE00000
SUMMON_GUID_MAX = 0xFFFFFF

# A pet walks back to its owner once it is farther than this and stops at the
# second distance.  ``SUMMON_LEASH_DISTANCE`` is also the maximum distance from
# the owner at which the pet will still chase an enemy.
SUMMON_FOLLOW_DISTANCE = 4.0
SUMMON_FOLLOW_STOP_DISTANCE = 2.0
SUMMON_LEASH_DISTANCE = 40.0

# Pets swing on their own timer (``Creature::GetAttackDistance``/melee delay).
SUMMON_PET_SWING_INTERVAL = 2.0

# One spline per tick, like the creature chase AI.
SUMMON_MOVE_MAX_MS = 1500


# ``MSG_MOVE_*``/``CMSG_MOVE_*`` opcodes all speak MovementInfo, but with
# slightly different framing.  Collect them once so the dispatcher only needs
# one membership test.
MOVEMENT_OPCODES = {
    int(opcode) for opcode in Opcodes
    if opcode.name.startswith(("MSG_MOVE_", "CMSG_MOVE_"))
}


# No packed guid in front of the movement info.
MOVEMENT_OPCODES_WITHOUT_GUID = {int(Opcodes.CMSG_MOVE_FALL_RESET)}


# Only an acknowledgement time, no position at all.
MOVEMENT_OPCODES_TIME_ONLY = {
    int(Opcodes.CMSG_MOVE_TIME_SKIPPED),
    int(Opcodes.CMSG_MOVE_NOT_ACTIVE_MOVER),
}


# Heartbeats arrive twice per second; do not log each of them at INFO.
MOVEMENT_OPCODES_QUIET = {
    int(Opcodes.MSG_MOVE_HEARTBEAT),
    int(Opcodes.MSG_MOVE_SET_FACING),
    int(Opcodes.MSG_MOVE_SET_PITCH),
}


# Text emote ID (from EmotesText.dbc) -> Emote ID (from Emotes.dbc / EmotesRec).
# Derived from the client's DBC data.  The client uses this mapping to play the
# correct animation when a text emote is received.  Only entries with a non-zero
# emote mapping are included; text emotes without an animation (e.g. /flex without
# a corresponding oneshot) get no animation.
TEXT_EMOTE_TO_EMOTE = {
    3: 14,      # angry -> ONESHOT_RUDE
    5: 21,      # applaud -> ONESHOT_APPLAUD
    6: 24,      # bashful -> ONESHOT_SHY
    8: 20,      # beg -> ONESHOT_BEG
    12: 24,     # blush -> ONESHOT_SHY
    17: 2,      # bow -> ONESHOT_BOW
    19: 3,      # bye -> ONESHOT_WAVE (DNR)
    20: 11,     # cackle -> ONESHOT_LAUGH
    21: 4,      # cheer -> ONESHOT_CHEER (DNR)
    22: 19,     # chicken -> ONESHOT_CHICKEN
    23: 11,     # chuckle -> ONESHOT_LAUGH
    24: 21,     # clap -> ONESHOT_APPLAUD
    25: 6,      # confused -> ONESHOT_QUESTION
    26: 5,      # congratulate -> ONESHOT_EXCLAMATION (DNR)
    28: 431,    # cower -> STATE_COWER
    31: 18,     # cry -> ONESHOT_CRY
    32: 6,      # curious -> ONESHOT_QUESTION
    33: 2,      # curtsey -> ONESHOT_BOW
    34: 10,     # dance -> STATE_DANCE
    35: 7,      # drink -> ONESHOT_EAT
    37: 7,      # eat -> ONESHOT_EAT
    41: 23,     # flex -> ONESHOT_FLEX
    43: 5,      # gasp -> ONESHOT_EXCLAMATION (DNR)
    47: 11,     # gloat -> ONESHOT_LAUGH
    48: 3,      # greet -> ONESHOT_WAVE (DNR)
    51: 20,     # grovel -> ONESHOT_BEG
    52: 11,     # guffaw -> ONESHOT_LAUGH
    53: 3,      # hail -> ONESHOT_WAVE (DNR)
    55: 3,      # hello -> ONESHOT_WAVE (DNR)
    58: 17,     # kiss -> ONESHOT_KISS
    59: 68,     # kneel -> STATE_KNEEL
    60: 11,     # laugh -> ONESHOT_LAUGH
    61: 12,     # laydown -> STATE_SLEEP
    64: 18,     # mourn -> ONESHOT_CRY
    66: 274,    # no -> ONESHOT_NO (DNR)
    67: 273,    # nod -> ONESHOT_YES (DNR)
    71: 20,     # plead -> ONESHOT_BEG
    72: 25,     # point -> ONESHOT_POINT (DNR)
    74: 16,     # pray -> ONESHOT_KNEEL
    75: 15,     # roar -> ONESHOT_ROAR (DNR)
    76: 11,     # rofl -> ONESHOT_LAUGH
    77: 14,     # rude -> ONESHOT_RUDE (DNR)
    78: 66,     # salute -> ONESHOT_SALUTE
    82: 22,     # shout -> ONESHOT_SHOUT (DNR)
    83: 6,      # shrug -> ONESHOT_QUESTION
    84: 24,     # shy -> ONESHOT_SHY
    85: 13,     # sit -> STATE_SIT
    87: 12,     # sleep -> STATE_SLEEP
    92: 20,     # surrender -> ONESHOT_BEG
    93: 1,      # talk -> ONESHOT_TALK (DNR)
    94: 5,      # talkex -> ONESHOT_EXCLAMATION (DNR)
    95: 6,      # talkq -> ONESHOT_QUESTION
    97: 1,      # thank -> ONESHOT_TALK (DNR)
    99: 4,      # victory -> ONESHOT_CHEER (DNR)
    101: 3,     # wave -> ONESHOT_WAVE (DNR)
    102: 1,     # welcome -> ONESHOT_TALK (DNR)
    106: 143,   # yawn -> ONESHOT_NONE (no anim)
    136: 19,    # taunt -> ONESHOT_CHICKEN
    141: 26,    # stand -> STATE_STAND
    143: 18,    # violin -> ONESHOT_CRY
    163: 0,     # smile -> no animation
    183: 14,    # rasp -> ONESHOT_RUDE (DNR)
    204: 15,    # growl -> ONESHOT_ROAR (DNR)
    223: 430,   # scared -> STATE_COWER
    243: 21,    # commend -> ONESHOT_APPLAUD
    264: 275,   # train -> ONESHOT_TRAIN (DNR)
    303: 22,    # helpme -> ONESHOT_SHOUT (DNR)
    304: 25,    # incoming -> ONESHOT_POINT (DNR)
    305: 25,    # charge -> ONESHOT_POINT (DNR)
    306: 22,    # flee -> ONESHOT_SHOUT (DNR)
    307: 15,    # attackmytarget -> ONESHOT_ROAR (DNR)
    323: 1,     # oom -> ONESHOT_TALK (DNR)
    324: 1,     # follow -> ONESHOT_TALK (DNR)
    325: 1,     # wait -> ONESHOT_TALK (DNR)
    326: 1,     # healme -> ONESHOT_TALK (DNR)
    327: 25,    # openfire -> ONESHOT_POINT (DNR)
    328: 24,    # flirt -> ONESHOT_SHY
    329: 1,     # joke -> ONESHOT_TALK (DNR)
    343: 21,    # golfclap -> ONESHOT_APPLAUD
    366: 377,   # mountspecial -> ONESHOT_MOUNTSPECIAL
    372: 274,   # disagree -> ONESHOT_NO (DNR)
    373: 274,   # doubt -> ONESHOT_NO (DNR)
    378: 7,     # toast -> ONESHOT_EAT
    379: 18,    # fail -> ONESHOT_CRY
    406: 20,    # mercy -> ONESHOT_BEG
    433: 1,     # sing -> ONESHOT_TALK (DNR)
    450: 25,    # object -> ONESHOT_POINT (DNR)
}


# Map emote text (from chat message) -> EmotesText.dbc ID
# Derived from the client's EmotesText.dbc (slash command column).
CHAT_EMOTE_TO_TEXT_ID = {
    "agree": 1, "amaze": 2, "angry": 3, "apologize": 4, "applaud": 5,
    "bashful": 6, "beckon": 7, "beg": 8, "bite": 9, "bleed": 10,
    "blink": 11, "blush": 12, "bonk": 13, "bored": 14, "bounce": 15,
    "brb": 16, "bow": 17, "burp": 18, "bye": 19, "cackle": 20,
    "cheer": 21, "chicken": 22, "chuckle": 23, "clap": 24, "confused": 25,
    "congratulate": 26, "cough": 27, "cower": 28, "crack": 29, "cringe": 30,
    "cry": 31, "curious": 32, "curtsey": 33, "dance": 34, "drink": 35,
    "drool": 36, "eat": 37, "eye": 38, "fart": 39, "fidget": 40,
    "flex": 41, "frown": 42, "gasp": 43, "gaze": 44, "giggle": 45,
    "glare": 46, "gloat": 47, "greet": 48, "grin": 49, "groan": 50,
    "grovel": 51, "guffaw": 52, "hail": 53, "happy": 54, "hello": 55,
    "hug": 56, "hungry": 57, "kiss": 58, "kneel": 59, "laugh": 60,
    "laydown": 61, "massage": 62, "moan": 63, "moon": 64, "mourn": 64,
    "no": 66, "nod": 67, "nosepick": 68, "panic": 69, "peer": 70,
    "plead": 71, "point": 72, "poke": 73, "pray": 74, "roar": 75,
    "rofl": 76, "rude": 77, "salute": 78, "scratch": 79, "sexy": 80,
    "shake": 81, "shout": 82, "shrug": 83, "shy": 84, "sigh": 85,
    "sit": 86, "sleep": 87, "snarl": 88, "spit": 89, "stare": 90,
    "surprised": 91, "surrender": 92, "talk": 93, "talkex": 94, "talkq": 95,
    "tap": 96, "thank": 97, "threaten": 98, "tired": 99, "victory": 100,
    "wave": 101, "welcome": 102, "whine": 103, "whistle": 104, "work": 105,
    "yawn": 106, "boggle": 107, "calm": 108, "cold": 109, "comfort": 110,
    "cuddle": 111, "duck": 112, "insult": 113, "introduce": 114, "jk": 115,
    "lick": 116, "listen": 117, "lost": 118, "mock": 119, "ponder": 120,
    "pounce": 121, "praise": 122, "purr": 123, "puzzle": 124, "raise": 125,
    "ready": 126, "shimmy": 127, "shiver": 128, "shoo": 129, "slap": 130,
    "smirk": 131, "sniff": 132, "snub": 133, "soothe": 134, "stink": 135,
    "taunt": 136, "tease": 137, "thirsty": 138, "veto": 139, "snicker": 140,
    "stand": 141, "tickle": 142, "violin": 143, "smile": 163, "rasp": 183,
    "pity": 203, "growl": 204, "bark": 205, "scared": 223, "flop": 224,
    "love": 225, "moo": 226, "commend": 243, "train": 264, "helpme": 303,
    "incoming": 304, "charge": 305, "flee": 306, "attackmytarget": 307,
    "oom": 323, "follow": 324, "wait": 325, "healme": 326, "openfire": 327,
    "flirt": 328, "joke": 329, "golfclap": 343, "wink": 363, "pat": 364,
    "serious": 365, "mountspecial": 366, "goodluck": 367, "blame": 368,
    "blank": 369, "brandish": 370, "breath": 371, "disagree": 372, "doubt": 373,
    "embarrass": 374, "encourage": 375, "enemy": 376, "eyebrow": 377,
    "toast": 378, "fail": 379, "highfive": 380, "absent": 381, "arm": 382,
    "awe": 383, "backpack": 384, "badfeeling": 385, "challenge": 386,
    "chug": 387, "ding": 389, "facepalm": 390, "faint": 391, "go": 392,
    "going": 393, "glower": 394, "headache": 395, "hiccup": 396, "hiss": 398,
    "holdhand": 399, "hurry": 401, "idea": 402, "jealous": 403, "luck": 404,
    "map": 405, "mercy": 406, "mutter": 407, "nervous": 408, "offer": 409,
    "pet": 410, "pinch": 411, "proud": 413, "promise": 414, "pulse": 415,
    "punch": 416, "pout": 417, "regret": 418, "revenge": 420, "rolleyes": 421,
    "ruffle": 422, "sad": 423, "scoff": 424, "scold": 425, "scowl": 426,
    "search": 427, "shakefist": 428, "shifty": 429, "shudder": 430,
    "signal": 431, "silence": 432, "sing": 433, "smack": 434, "sneak": 435,
    "sneeze": 436, "snort": 437, "squeal": 438, "stopattack": 439,
    "suspicious": 440, "think": 441, "truce": 442, "twiddle": 443, "warn": 444,
    "snap": 445, "charm": 446, "coverears": 447, "crossarms": 448, "look": 449,
    "object": 450, "sweat": 451,
}


# State emote IDs (from Emotes.dbc specProc != 0). These use the NPC emote state
# system (UNIT_NPC_EMOTESTATE) and do NOT get SMSG_EMOTE.
# Derived from the client's Emotes.dbc.
STATE_EMOTE_IDS = {
    10, 12, 13, 26, 27, 28, 29, 30, 64, 65, 68, 69, 93, 133, 173, 193,
    214, 233, 234, 253, 313, 333, 353, 373, 375, 376, 378, 379, 382, 383,
    384, 385, 391, 392, 398, 400, 412, 413, 415, 416, 417, 418, 419, 420,
    421, 422, 423, 424, 425, 426, 427, 428, 429, 431, 433, 437, 438, 444,
    446, 451, 453, 455, 456,
}

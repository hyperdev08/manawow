"""3.3.5a (build 12340) update-field indices used when creating a player.

Values taken from CMaNGOS' ``UpdateFields.h``, which is generated for
"version 3, 3, 5, 12340".  Only the fields we actually populate on login are
listed; everything else stays at the client's default of zero.
"""
from __future__ import annotations

# --- Object (absolute indices) ------------------------------------------
OBJECT_FIELD_GUID = 0x0000          # uint64
OBJECT_FIELD_TYPE = 0x0002          # uint32
OBJECT_FIELD_ENTRY = 0x0003         # uint32
OBJECT_FIELD_SCALE_X = 0x0004       # float

# --- Unit ---------------------------------------------------------------
# Relationship fields (3.3.5a ``UpdateFields.h``).  GUID fields occupy two
# uint32 slots, which is why ``UNIT_FIELD_BYTES_0`` lands at 23:
#   CHARM 6-7, SUMMON 8-9, CRITTER 10-11, CHARMEDBY 12-13,
#   SUMMONEDBY 14-15, CREATEDBY 16-17, TARGET 18-19, CHANNEL_OBJECT 20-21,
#   CHANNEL_SPELL 22, BYTES_0 23.
UNIT_FIELD_CHARM = 6                # uint64, charmer guid
UNIT_FIELD_SUMMON = 8               # uint64, summoned unit guid
UNIT_FIELD_CRITTER = 10             # uint64, minipet guid
UNIT_FIELD_CHARMEDBY = 12           # uint64, charmer of this unit
UNIT_FIELD_SUMMONEDBY = 14          # uint64, owner of a summon/pet
UNIT_FIELD_CREATEDBY = 16           # uint64, creator of a summon
UNIT_FIELD_TARGET = 18              # uint64, current attack target
UNIT_FIELD_BYTES_0 = 23             # race | class<<8 | gender<<16 | power<<24
UNIT_FIELD_HEALTH = 24
UNIT_FIELD_POWER1 = 25
UNIT_FIELD_POWER2 = 26
UNIT_FIELD_POWER3 = 27
UNIT_FIELD_POWER4 = 28
UNIT_FIELD_POWER5 = 29
UNIT_FIELD_POWER6 = 30
UNIT_FIELD_POWER7 = 31
UNIT_FIELD_MAXHEALTH = 32
UNIT_FIELD_MAXPOWER1 = 33
UNIT_FIELD_MAXPOWER2 = 34
UNIT_FIELD_MAXPOWER3 = 35
UNIT_FIELD_MAXPOWER4 = 36
UNIT_FIELD_MAXPOWER5 = 37
UNIT_FIELD_MAXPOWER6 = 38
UNIT_FIELD_MAXPOWER7 = 39
# Base attributes (TrinityCore 3.3.5 UpdateFields.h, OBJECT_END = 6):
#   UNIT_FIELD_STAT0..4   = 84..88
#   UNIT_FIELD_POSSTAT0..4 = 89..93
#   UNIT_FIELD_NEGSTAT0..4 = 94..98
UNIT_FIELD_STAT0 = 84               # strength
UNIT_FIELD_STAT1 = 85               # agility
UNIT_FIELD_STAT2 = 86               # stamina
UNIT_FIELD_STAT3 = 87               # intellect
UNIT_FIELD_STAT4 = 88               # spirit
UNIT_FIELD_POSSTAT0 = 89
UNIT_FIELD_POSSTAT1 = 90
UNIT_FIELD_POSSTAT2 = 91
UNIT_FIELD_POSSTAT3 = 92
UNIT_FIELD_POSSTAT4 = 93
UNIT_FIELD_NEGSTAT0 = 94
UNIT_FIELD_NEGSTAT1 = 95
UNIT_FIELD_NEGSTAT2 = 96
UNIT_FIELD_NEGSTAT3 = 97
UNIT_FIELD_NEGSTAT4 = 98
# ``UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER``/``..._INTERRUPTED_FLAT_MODIFIER``
# are float[7] blocks (one entry per power type) the owner's client uses to
# animate the power bar and print the "5 second mana regen" number.
UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER = 40
UNIT_FIELD_POWER_REGEN_INTERRUPTED_FLAT_MODIFIER = 47
# Resistance block: 7 ints (physical armor, holy, fire, nature, frost, shadow,
# arcane), from CMaNGOS/TrinityCore UpdateFields.h.
UNIT_FIELD_RESISTANCES = 99
UNIT_FIELD_RESISTANCEBUFFMODSPOSITIVE = 106
UNIT_FIELD_RESISTANCEBUFFMODSNEGATIVE = 113
UNIT_FIELD_BASE_MANA = 120
UNIT_FIELD_BASE_HEALTH = 121
# Attack power: the client reads these for the character sheet's Attack Power
# number and its tooltip.  MODS is a packed pair of int16 (positive, negative)
# and MULTIPLIER is the *bonus* fraction (0.0 = no bonus).
UNIT_FIELD_ATTACK_POWER = 123
UNIT_FIELD_ATTACK_POWER_MODS = 124
UNIT_FIELD_ATTACK_POWER_MULTIPLIER = 125
UNIT_FIELD_RANGED_ATTACK_POWER = 126
UNIT_FIELD_RANGED_ATTACK_POWER_MODS = 127
UNIT_FIELD_RANGED_ATTACK_POWER_MULTIPLIER = 128
UNIT_FIELD_LEVEL = 54
UNIT_FIELD_FACTIONTEMPLATE = 55
UNIT_FIELD_FLAGS = 59
UNIT_FIELD_FLAGS_2 = 60             # uint32, bit 0x800 = UNIT_FLAG2_REGENERATE_POWER
UNIT_FIELD_BOUNDINGRADIUS = 65
UNIT_FIELD_COMBATREACH = 66
UNIT_FIELD_DISPLAYID = 67
UNIT_FIELD_NATIVEDISPLAYID = 68
UNIT_FIELD_MOUNTDISPLAYID = 69
UNIT_FIELD_BYTES_1 = 74
UNIT_DYNAMIC_FLAGS = 79
UNIT_NPC_FLAGS = 82                # uint32, bit 0 = UNIT_NPC_FLAG_GOSSIP
UNIT_NPC_EMOTESTATE = 83
UNIT_FIELD_BYTES_2 = 122

# UNIT_END is the first player-field index; a creature's update mask always
# covers exactly this many fields (5 mask blocks).
UNIT_END = 148

# --- Player -------------------------------------------------------------
# Player fields are relative to UNIT_END (148).  The absolute indices below
# were verified against the decompiled Mirror.cpp descriptor table where
# PLAYER_FIELD_INV_SLOT_HEAD has offset 176 within the player block and
# PLAYER_START = UNIT_END = 148, giving absolute index 148 + 176 = 324.
PLAYER_START = UNIT_END
PLAYER_FLAGS = 150
PLAYER_FLAGS_GHOST = 0x00000010
PLAYER_BYTES = 153                  # skin | face<<8 | hair style<<16 | hair colour<<24
PLAYER_BYTES_2 = 154                # facial style in the low byte
PLAYER_BYTES_3 = 155                # gender in the low byte
# 19 visible-item pairs (entry id + enchant visual), stride 2.  Other players
# need these to render equipped gear; the client has no other way to learn it.
PLAYER_VISIBLE_ITEM_1_ENTRYID = 283
PLAYER_CHOSEN_TITLE = 321
PLAYER_FIELD_INV_SLOT_HEAD = 324    # 46 fields: 19 equip + 4 bag slots (uint64 guids)
PLAYER_FIELD_PACK_SLOT_1 = 370      # 32 fields: 16 backpack slots (uint64 guids)
PLAYER_XP = 634
PLAYER_NEXT_LEVEL_XP = 635
PLAYER_FIELD_COINAGE = 1170
PLAYER_FIELD_MAX_LEVEL = 1279

# PLAYER_END for 3.3.5a; the update mask always covers this many fields.
PLAYER_VALUES_COUNT = 1326

# Player skill fields: PLAYER_SKILL_INFO_1_1 is offset 488 within the player
# block (Mirror.cpp), so its absolute index is PLAYER_START + 488 = 636.
# 128 skills * 3 uint32 fields each (id|step, rank|maxRank, temp|permBonus) = 384.
PLAYER_SKILL_INFO_1_1 = PLAYER_START + 488    # absolute index 636
PLAYER_SKILL_COUNT = 128

# Item high guid for 3.3.5 (TrinityCore ``HIGHGUID_ITEM``).  Player guids are the
# bare low guid, so items live in a completely separate guid range and the client
# resolves them with a different object type.
ITEM_HIGH_GUID = 0x4000000000000000

# Creature high guid (TrinityCore ``HIGHGUID_UNIT = 0xF130`` in the top 16 bits,
# i.e. ``0xF130 << 48``).  Keeping creatures in their own guid range means a
# creature can never collide with a player whose low guid happens to match.
CREATURE_HIGH_GUID = 0xF130000000000000

# --- Item (relative to OBJECT_END = 6) ----------------------------------
ITEM_START = 6
ITEM_FIELD_OWNER = 6               # uint64
ITEM_FIELD_CONTAINED = 8           # uint64
ITEM_FIELD_CREATOR = 10            # uint64
ITEM_FIELD_GIFTCREATOR = 12        # uint64
ITEM_FIELD_STACK_COUNT = 14        # uint32
ITEM_FIELD_DURATION = 15           # uint32
ITEM_FIELD_SPELL_CHARGES = 16      # 5 x int32
ITEM_FIELD_FLAGS = 21              # uint32
ITEM_FIELD_ENCHANTMENT_1_1 = 22    # enchantment blocks (12 enchants, 3 fields each)
ITEM_FIELD_PROPERTY_SEED = 58      # uint32
ITEM_FIELD_RANDOM_PROPERTIES_ID = 59  # uint32
ITEM_FIELD_DURABILITY = 60         # uint32
ITEM_FIELD_MAXDURABILITY = 61      # uint32
ITEM_FIELD_CREATE_PLAYED_TIME = 62 # uint32
ITEM_END = 64

# --- GameObject (relative to OBJECT_END = 6) ----------------------------
OBJECT_FIELD_CREATED_BY = 6        # uint64
GAMEOBJECT_DISPLAYID = 8           # uint32
GAMEOBJECT_FLAGS = 9               # uint32
GAMEOBJECT_PARENTROTATION = 10     # 4 x float (x, y, z, w)
GAMEOBJECT_DYNAMIC = 14            # uint16 dyn flags | uint16 path progress
GAMEOBJECT_FACTION = 15            # uint32
GAMEOBJECT_LEVEL = 16              # uint32
GAMEOBJECT_BYTES_1 = 17            # state, type, art kit, anim progress
GAMEOBJECT_END = 18

# --- Corpse (relative to OBJECT_END = 6) --------------------------------
CORPSE_FIELD_OWNER = 6             # uint64
CORPSE_FIELD_PARTY = 8             # uint64
CORPSE_FIELD_DISPLAY_ID = 10       # uint32
CORPSE_FIELD_ITEM = 11             # 19 x uint32 (equipment item entries)
CORPSE_FIELD_BYTES_1 = 30          # skin | face<<8 | hair<<16 | colour<<24
CORPSE_FIELD_BYTES_2 = 31          # facial style in the low byte
CORPSE_FIELD_GUILD = 32            # uint32
CORPSE_FIELD_FLAGS = 33            # uint32
CORPSE_FIELD_DYNAMIC_FLAGS = 34    # uint32
CORPSE_END = 36

# Type masks for OBJECT_FIELD_TYPE
TYPEMASK_OBJECT = 0x0001
TYPEMASK_ITEM = 0x0002
TYPEMASK_CONTAINER = 0x0004
TYPEMASK_UNIT = 0x0008
TYPEMASK_PLAYER = 0x0010
TYPEMASK_GAMEOBJECT = 0x0020
TYPEMASK_CORPSE = 0x0080

# Corpse object guids use HighGuid::Corpse (0xF101 in 3.3.5).
CORPSE_HIGH_GUID = 0xF101000000000000

# Inventory slot constants (client-side slot numbers)
INVSLOT_HEAD = 0
INVSLOT_NECK = 1
INVSLOT_SHOULDERS = 2
INVSLOT_BODY = 3
INVSLOT_CHEST = 4
INVSLOT_WAIST = 5
INVSLOT_LEGS = 6
INVSLOT_FEET = 7
INVSLOT_WRISTS = 8
INVSLOT_HANDS = 9
INVSLOT_FINGER1 = 10
INVSLOT_FINGER2 = 11
INVSLOT_TRINKET1 = 12
INVSLOT_TRINKET2 = 13
INVSLOT_BACK = 14
INVSLOT_MAINHAND = 15
INVSLOT_OFFHAND = 16
INVSLOT_RANGED = 17
INVSLOT_TABARD = 18
INVENTORY_SLOT_BAG_0 = 19          # backpack itself
INVENTORY_SLOT_BAG_1 = 20
INVENTORY_SLOT_BAG_2 = 21
INVENTORY_SLOT_BAG_3 = 22
INVENTORY_SLOT_BAG_END = 23

# Backpack slots 0..15 map to PACK_SLOT_1
BAG_SLOT_0 = 23                    # first backpack content slot
BAG_SLOT_15 = 38                   # last backpack content slot

# Item GUID high part for 3.3.5a: item counter in low 32 bits, high guid in upper bits
ITEM_HIGH_GUID = 0x4000000000000000

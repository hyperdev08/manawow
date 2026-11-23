"""The 3.3.5a packet burst sent when the client enters the world.

Mirrors the essential part of TrinityCore's ``WorldSession::HandlePlayerLogin``
plus ``Player::SendInitialPacketsBeforeAddToMap``/``AfterAddToMap`` and
``Player::UpdateZone``.  Systems we do not simulate yet (guilds, achievements,
talents, action bars, spells) are still announced with empty packets so the
client's UI is initialised instead of waiting for data that never arrives.
"""
from __future__ import annotations

import struct
import time
from typing import List, Mapping, Tuple

from src.protocol.opcodes import Opcodes
from src.world import object_update, creature
from src.world.character_db import default_sheath_type

NUM_ACCOUNT_DATA_TYPES = 8
MAX_ACTION_BUTTONS = 144
TIME_SPEED = 0.01666667


def _packed_wow_time(now: float | None = None) -> int:
    """TrinityCore ``GameTime::GetWowTime`` packing."""
    t = time.localtime(now)
    year = max(0, t.tm_year - 2000)
    # Python: Monday=0; WoW uses Sunday=0.
    weekday = (t.tm_wday + 1) % 7
    return (
        ((year & 0x1F) << 24)
        | ((t.tm_mon & 0x0F) << 20)
        | ((t.tm_mday & 0x3F) << 14)
        | ((weekday & 0x07) << 11)
        | ((t.tm_hour & 0x1F) << 6)
        | (t.tm_min & 0x3F)
    )


def build_login_verify_world(char: Mapping) -> bytes:
    x = float(char["x"])
    y = float(char["y"])
    z = float(char["z"])
    o = float(char["o"]) if "o" in char.keys() else 0.0
    return struct.pack("<Iffff", int(char["map"]), x, y, z, o)


def build_account_data_times(mask: int = 0xFF, server_time: int = 0) -> bytes:
    """``SMSG_ACCOUNT_DATA_TIMES``: server time, unknown byte, mask, times.

    The mask is the important part.  On entering the world the client calls
    ``AccountDataInitialize``, which marks every account-data type of the
    current context as ``m_downloading``, and ``CGUIBindings::LoadBindings``
    defers its callback until the matching ``SMSG_ACCOUNT_DATA_TIMES`` record
    clears that flag.  A zero mask skips every type, so ``FinishLoadBindings``
    never runs: the *current* key-binding set stays empty and every key
    (movement, ESC, C, M...) silently does nothing even though key events are
    delivered.  Announce all eight types, as TrinityCore does.

    ``server_time`` is deliberately ``0``: it makes the client treat its local
    cache as authoritative instead of waiting for a ``SMSG_UPDATE_ACCOUNT_DATA``
    download that this server does not need to send.
    """
    payload = struct.pack("<IBI", server_time, 0, mask & 0xFFFFFFFF)
    for index in range(NUM_ACCOUNT_DATA_TYPES):
        if mask & (1 << index):
            payload += struct.pack("<I", server_time)
    return payload


def build_motd(motd: str) -> bytes:
    # SMSG_MOTD is a uint32 line count followed by NUL-terminated lines.  The
    # real core splits its config string on '@'.
    lines = [line for line in (motd or "").split("@") if line]
    payload = struct.pack("<I", len(lines))
    for line in lines:
        payload += line.encode("utf-8", errors="replace") + b"\x00"
    return payload


def build_feature_system_status() -> bytes:
    # uint8 can-complain (2 = enabled, auto-ignore), uint8 voice chat enabled
    return struct.pack("<BB", 2, 0)


def build_bind_point(char: Mapping) -> bytes:
    """``SMSG_BIND_POINT_UPDATE`` (0x155).

    TrinityCore's ``BindPointUpdate::Write`` sends a ``WorldLocation``
    (uint32 mapId + float x/y/z) followed by the bind area id.  The client
    uses it to fill in the hearthstone tooltip and the death-repop location.
    """
    return struct.pack(
        "<IfffI",
        int(char["map"]),
        float(char["x"]), float(char["y"]), float(char["z"]),
        int(char["zone"]),
    )


def build_initial_spells(char: Mapping, known_spell_ids: Optional[List[int]] = None) -> bytes:
    """``SMSG_INITIAL_SPELLS`` (0x12A) - spells the player knows on login.

    Wire format: uint8 initial_login, uint16 spell_count,
    [uint32 spell_id, uint16 slot] * count, uint16 cooldown_count,
    [cooldown entries].

    ``initial_login`` mirrors TrinityCore's ``InitialLogin = IsLoading()``.
    The 3.3.5 client only rebuilds its spellbook from this packet when the flag
    is 1; with 0 it treats the packet as a partial resync and the spells never
    show up in the spellbook or on the action bar.

    The known spells come from the authentic ``playercreateinfo_spell`` data,
    which already contains the character's racial languages (Language Common,
    Orcish, ...).  The client builds ``m_languageSpells`` from those
    ``SPELL_EFFECT_LANGUAGE`` spells, exactly like the reference core.
    """
    if known_spell_ids is None:
        known_spell_ids = []

    spell_ids = list(dict.fromkeys(int(s) for s in known_spell_ids))

    out = bytearray()
    out += struct.pack("<B", 1)          # initial login (rebuild spellbook)
    out += struct.pack("<H", len(spell_ids))  # spell count
    for spell_id in spell_ids:
        out += struct.pack("<IH", spell_id, 0)  # spell_id, slot (0 = not on action bar)
    out += struct.pack("<H", 0)          # cooldown count = 0
    return bytes(out)


def build_action_buttons(buttons: Optional[dict] = None) -> bytes:
    """``SMSG_ACTION_BUTTONS`` with one action per bar slot.

    ``UpdateActionButtons::Write`` sends ``uint8 reason`` followed by exactly
    144 packed uint32 buttons.  A spell button is ``(action & 0xFFFFFF) |
    (type << 24)``; ``type`` is 0 for a spell and 128 for a macro.  The usual
    argument is the ``{slot: (action, type)}`` mapping returned by
    ``get_character_action_buttons``.  A plain list of spell ids is still
    accepted and placed from slot 0.
    TrinityCore sends reason 1 for the initial action buttons; the client then
    validates that every spell is present in ``SMSG_INITIAL_SPELLS``, which is
    why that packet has to be sent first.
    """
    slots = [0] * MAX_ACTION_BUTTONS
    if isinstance(buttons, dict):
        for index, value in buttons.items():
            index = int(index)
            if not 0 <= index < MAX_ACTION_BUTTONS:
                continue
            action, action_type = value
            slots[index] = ((int(action) & 0xFFFFFF)
                            | ((int(action_type) & 0xFF) << 24))
    else:
        for index, spell_id in enumerate(buttons or []):
            if index >= MAX_ACTION_BUTTONS:
                break
            slots[index] = (int(spell_id) & 0xFFFFFF) | (0 << 24)
    out = bytearray([1])                 # reason: initial action buttons
    out += struct.pack(f"<{MAX_ACTION_BUTTONS}I", *slots)
    return bytes(out)


def build_initialize_factions() -> bytes:
    # uint32 faction count, then count * (uint8 flags, uint32 standing).
    # An empty reputation list would leave the client's tab uninitialised, so
    # send the full 128 neutral slots like the real core does.
    payload = struct.pack("<I", 128)
    payload += bytes(128 * 5)
    return payload


def build_init_world_states(char: Mapping) -> bytes:
    payload = struct.pack(
        "<IIIH", int(char["map"]), int(char["zone"]), 0, 0
    )
    return payload


def build_login_set_time_speed(now: float | None = None) -> bytes:
    return struct.pack("<IfI", _packed_wow_time(now), TIME_SPEED, 0)


def build_force_run_speed(guid: int, speed: float) -> bytes:
    """``SMSG_FORCE_RUN_SPEED_CHANGE``: packed guid, uint32 0, float speed."""
    return (object_update.pack_guid(int(guid))
            + struct.pack("<If", 0, float(speed)))


def build_force_move_root(guid: int) -> bytes:
    """``SMSG_FORCE_MOVE_ROOT``: packed guid, uint32 0."""
    return object_update.pack_guid(int(guid)) + struct.pack("<I", 0)


def build_force_move_unroot(guid: int) -> bytes:
    """``SMSG_FORCE_MOVE_UNROOT``: packed guid, uint32 0."""
    return object_update.pack_guid(int(guid)) + struct.pack("<I", 0)


def build_name_query_response(char: Mapping) -> bytes:
    """``SMSG_NAME_QUERY_RESPONSE`` for a character on this realm.

    packed guid, uint8 result (0 = found), name, realm name (empty when the
    character is on the queried realm), race, gender, class, declined names.
    """
    return (
        object_update.pack_guid(int(char["guid"]))
        + b"\x00"
        + str(char["name"]).encode("utf-8", errors="replace") + b"\x00"
        + b"\x00"
        + bytes([
            int(char["race"]) & 0xFF,
            int(char["gender"]) & 0xFF,
            int(char["class"]) & 0xFF,
            0,  # no declined names
        ])
    )


def build_creature_name_query_response(template: Mapping) -> bytes:
    """``SMSG_NAME_QUERY_RESPONSE`` for a creature.

    Layout is identical to the player response, but the guid is the
    ``creature_spawns`` low GUID promoted to a full object GUID via
    ``creature_guid`` (i.e. ``CREATURE_HIGH_GUID | low_guid``).  The name,
    race, gender and class come from ``creature_template``.
    """
    low_guid = int(template.get("low_guid", 0))
    if low_guid == 0:
        raise ValueError("creature template missing low_guid")
    return (
        object_update.pack_guid(
            creature.creature_guid(low_guid, int(template.get("entry", 0)))
        )
        + b"\x00"
        + creature_name(template).encode("utf-8", errors="replace") + b"\x00"
        + b"\x00"
        + bytes([
            int(template.get("unit_gender", 0)) & 0xFF,  # race -> gender field
            int(template.get("unit_gender", 0)) & 0xFF,  # gender
            int(template.get("unit_class", 0)) & 0xFF,   # class
            0,  # no declined names
        ])
    )


def build_name_query_failure(guid: int) -> bytes:
    return object_update.pack_guid(guid) + b"\x01"


def _seconds_until_daily_reset(now: float | None = None) -> int:
    """Seconds until the next 03:00 local time (TrinityCore's reset hour)."""
    now = time.time() if now is None else now
    local = time.localtime(now)
    reset = time.mktime((
        local.tm_year, local.tm_mon, local.tm_mday, 3, 0, 0, 0, 0, -1,
    ))
    if reset <= now:
        reset += 24 * 60 * 60
    return int(reset - now)


def build_query_time_response(now: float | None = None) -> bytes:
    # uint32 server time, int32 seconds until the next daily quest reset.
    now = time.time() if now is None else now
    return struct.pack("<Ii", int(now), _seconds_until_daily_reset(now))


def build_played_time_response(total_seconds: int = 0, level_seconds: int = 0) -> bytes:
    # uint32 total /played, uint32 /played at the current level.
    return struct.pack("<II", int(total_seconds), int(level_seconds))


def build_item_query_not_found(item_id: int) -> bytes:
    """Report an unknown item: the id with the high bit set and no stats.

    ``DBCache<T>::AddItems`` clears the cached record and fails every pending
    callback when it sees ``int32(id) < 0``.
    """
    return struct.pack("<I", (int(item_id) & 0x7FFFFFFF) | 0x80000000)


def _cstring(value: object) -> bytes:
    """A NUL-terminated string, the way ``CDataStore::GetString`` reads them."""
    if value is None:
        value = ""
    return str(value).encode("utf-8", errors="replace") + b"\x00"


def build_item_query_response(template) -> bytes:
    """``SMSG_ITEM_QUERY_SINGLE_RESPONSE`` for one item template row.

    ``ItemStats_C::Unpack`` (WoW/Source/Object/ItemStats.cpp) defines the exact
    order; ``DBCache<T>::AddItems(msg, single = true)`` reads the ``uint32``
    entry id first and then hands the rest of the stream to ``Unpack``, so the
    template fields follow directly with no further framing.

    ``template`` is a mapping (an ``sqlite3.Row`` is fine) whose keys are the
    ``item_templates`` column names.
    """
    def field(name: str, default: int = 0) -> int:
        try:
            value = template[name]
        except (KeyError, IndexError):
            return default
        return int(value) if value is not None else default

    def field_u(name: str, default: int = 0) -> int:
        """Field masked to unsigned 32-bit for 'I' format."""
        try:
            value = template[name]
        except (KeyError, IndexError):
            return default
        if value is None:
            return default
        v = int(value)
        return v & 0xFFFFFFFF if v < 0 else v

    def real(name: str, default: float = 0.0) -> float:
        try:
            value = template[name]
        except (KeyError, IndexError):
            return default
        return float(value) if value is not None else default

    def text(name: str) -> bytes:
        try:
            value = template[name]
        except (KeyError, IndexError):
            value = ""
        return _cstring(value)

    out = bytearray()
    out += struct.pack("<I", field_u("entry") & 0x7FFFFFFF)

    out += struct.pack("<iii", field("class"), field("subclass"),
                       field("sound_override_subclass"))
    for index in range(1, 5):
        out += text(f"name{index}")

    out += struct.pack(
        "<IIII", field_u("display_id"), field_u("quality"),
        field_u("flags1"), field_u("flags2"),
    )
    # buyPrice is read into an int32, sellPrice into a uint32.
    out += struct.pack("<ii", field("buy_price"), field_u("sell_price") & 0xFFFFFFFF)
    out += struct.pack(
        "<IIIIIIIIIIIIIII",
        field_u("inventory_type"), field_u("allowable_class"), field_u("allowable_race"),
        field_u("item_level"), field_u("required_level"), field_u("required_skill"),
        field_u("required_skill_rank"), field_u("required_spell"),
        field_u("required_honor_rank"), field_u("required_city_rank"),
        field_u("required_reputation_faction"), field_u("required_reputation_rank"),
        field_u("max_count"), field_u("stackable", 1), field_u("container_slots"),
    )

    # The client reads exactly statsCount (type, value) pairs and fills the
    # remainder of its ten slots with -1 itself, so only send the real ones.
    stats_count = max(0, min(10, field("stats_count")))
    out += struct.pack("<i", stats_count)
    for index in range(1, stats_count + 1):
        out += struct.pack(
            "<ii", field(f"stat_type{index}"), field(f"stat_value{index}")
        )

    out += struct.pack("<II", field_u("scaling_stat_distribution"), field_u("scaling_stat_value"))

    for suffix in (1, 2):
        out += struct.pack(
            "<ffI", real(f"damage_min{suffix}"), real(f"damage_max{suffix}"),
            field_u(f"damage_type{suffix}"),
        )

    out += struct.pack(
        "<IIIIIII",
        field_u("resistance_armor"), field_u("resistance_holy"), field_u("resistance_fire"),
        field_u("resistance_nature"), field_u("resistance_frost"),
        field_u("resistance_shadow"), field_u("resistance_arcane"),
    )

    # m_delay is an int32; the client treats <= 0 as "one second".
    out += struct.pack("<i", field("delay"))
    out += struct.pack("<I", field_u("ammo_type"))
    out += struct.pack("<f", real("ranged_mod_range"))

    for suffix in (1, 2, 3, 4, 5):
        out += struct.pack(
            "<IIiiII",
            field_u(f"spell_id{suffix}"), field_u(f"spell_trigger{suffix}"),
            field(f"spell_charges{suffix}"), field(f"spell_cooldown{suffix}"),
            field_u(f"spell_category{suffix}"), field_u(f"spell_category_cooldown{suffix}"),
        )

    out += struct.pack("<I", field_u("bonding"))
    out += text("description")
    # Item.dbc sheath type.  With the client's default sheathed weapon mode a
    # value of 0 resolves to ATTACH_NONE, so an equipped weapon is never
    # attached to the model (invisible sword).  Derive it when a template
    # predates the column instead of silently sending 0.
    sheath_type = field_u("sheath_type") or default_sheath_type(field_u("inventory_type"))
    out += struct.pack(
        "<IIIIIIIIIIIIII",
        field_u("page_text"), field_u("language_id"), field_u("page_material"),
        field_u("start_quest"), field_u("lock_id"), field_u("material"),
        sheath_type, field_u("random_property"), field_u("random_suffix"),
        field_u("block"), field_u("item_set"), field_u("max_durability"),
        field_u("area"), field_u("map"),
    )
    out += struct.pack(
        "<II", field_u("bag_family"), field_u("totem_category"),
    )
    for index in (1, 2, 3):
        out += struct.pack(
            "<II", field_u(f"socket_color_{index}"), field_u(f"socket_content_{index}")
        )
    out += struct.pack(
        "<IIIfII",
        field_u("socket_bonus"), field_u("gem_properties"),
        field_u("required_disenchant_skill"), real("armor_damage_modifier"),
        field_u("duration"), field_u("item_limit_category"),
    )
    out += struct.pack("<I", field_u("holiday_id"))

    return bytes(out)


def build_calendar_num_pending(count: int = 0) -> bytes:
    return struct.pack("<I", int(count))


# ---------------------------------------------------------------------------
# Creatures, gossip and NPC text
# ---------------------------------------------------------------------------
def build_creature_query_not_found(entry: int) -> bytes:
    """Report an unknown creature: the entry with the high bit set."""
    return struct.pack("<I", (int(entry) & 0x7FFFFFFF) | 0x80000000)


def creature_name(template: Mapping) -> str:
    """Return the creature's plain name (what 3.3.5a draws on nameplates)."""
    try:
        return str(template["name"] or "")
    except (KeyError, IndexError):
        return ""


def creature_title(template: Mapping) -> str:
    """Return the NPC's job title/affiliation for the tooltip line.

    The 3.3.5a client renders ``name`` on the nameplate and uses the separate
    ``title`` field for the ``<Role>`` line in the mouseover tooltip, exactly
    like vanilla.  Placeholder rows whose subname already carries angle
    brackets are unwrapped so the client does not draw ``<<...>>``.
    """
    try:
        title = str(template["subname"] or "").strip()
    except (KeyError, IndexError):
        return ""
    if title.startswith("<") and title.endswith(">"):
        title = title[1:-1].strip()
    return title


def build_creature_query_response(template) -> bytes:
    """``SMSG_CREATURE_QUERY_RESPONSE`` for one ``creature_templates`` row.

    Layout from TrinityCore ``QueryCreatureResponse::Write`` (3.3.5):
    entry, name, three empty name variants, title, cursor name, flags, type,
    family, classification, 2 kill-credit entries, 4 display ids, two
    multipliers, leader byte, 6 quest items and the movement-info id.

    ``name`` is the plain creature name (nameplates) and ``title`` carries the
    subname so the mouseover tooltip shows the vanilla ``Name`` / ``<Role>``
    pair.
    """
    def field(name: str, default: int = 0) -> int:
        try:
            value = template[name]
        except (KeyError, IndexError):
            return default
        return int(value) if value is not None else default

    def real(name: str, default: float = 1.0) -> float:
        try:
            value = template[name]
        except (KeyError, IndexError):
            return default
        return float(value) if value is not None else default

    out = bytearray()
    out += struct.pack("<I", field("entry") & 0x7FFFFFFF)
    out += _cstring(creature_name(template))
    out += b"\x00\x00\x00"                       # name2, name3, name4
    # The title is the vanilla "<Role>" line in the mouseover tooltip.
    out += _cstring(creature_title(template))
    # Cursor name: "Directions" for guards, otherwise empty.
    out += _cstring(template["cursor_name"] if "cursor_name" in template.keys() else "")
    out += struct.pack(
        "<IIII",
        field("flags"), field("type"), field("family"), field("classification"),
    )
    out += struct.pack("<II", field("kill_credit1"), field("kill_credit2"))
    out += struct.pack(
        "<IIII",
        field("display_id1"), field("display_id2"),
        field("display_id3"), field("display_id4"),
    )
    out += struct.pack(
        "<ffB",
        real("hp_multiplier"), real("energy_multiplier"), field("leader") & 0xFF,
    )
    out += struct.pack(
        "<IIIIII",
        field("quest_item1"), field("quest_item2"), field("quest_item3"),
        field("quest_item4"), field("quest_item5"), field("quest_item6"),
    )
    out += struct.pack("<I", field("movement_info_id"))
    return bytes(out)


def build_gossip_message(guid: int, gossip_id: int, text_id: int,
                         options=(), quests=()) -> bytes:
    """``SMSG_GOSSIP_MESSAGE`` as read by the client's GossipInfo.cpp.

    uint64 guid, int32 gossip menu id, int32 NPC-text id, uint32 option count,
    options (uint32 index, uint8 type, uint8 coded, uint32 box money, text,
    box text), then uint32 quest count and the quests (id, icon, level, flags,
    uint8 repeatable, title).
    """
    out = bytearray()
    out += struct.pack("<Q", int(guid) & 0xFFFFFFFFFFFFFFFF)
    out += struct.pack("<ii", int(gossip_id), int(text_id))

    options = list(options)
    out += struct.pack("<I", len(options))
    for option in options:
        def field(name: str, default: int = 0) -> int:
            try:
                value = option[name]
            except (KeyError, IndexError):
                return default
            return int(value) if value is not None else default

        def text(name: str) -> bytes:
            try:
                value = option[name]
            except (KeyError, IndexError):
                value = ""
            return _cstring(value)

        out += struct.pack("<I", field("option_index"))
        out += bytes([field("option_type") & 0xFF, field("coded") & 0xFF])
        out += struct.pack("<I", field("box_money") & 0xFFFFFFFF)
        out += text("text")
        out += text("box_text")

    quests = list(quests)
    out += struct.pack("<I", len(quests))
    for quest in quests:
        def qfield(name: str, default: int = 0) -> int:
            try:
                value = quest[name]
            except (KeyError, IndexError):
                return default
            return int(value) if value is not None else default

        out += struct.pack(
            "<IIII", qfield("quest_id"), qfield("icon"),
            qfield("level"), qfield("flags"),
        )
        out += bytes([qfield("repeatable") & 0xFF])
        try:
            title = quest["title"]
        except (KeyError, IndexError):
            title = ""
        out += _cstring(title)
    return bytes(out)


def build_npc_text_update(text_id: int, text=None) -> bytes:
    """``SMSG_NPC_TEXT_UPDATE`` with the eight options the client always reads.

    ``QueryNPCTextResponse::Write`` sends ``textId | allow-bit`` followed by
    NPCText records: float probability, male text, female text, int32 language
    and three (uint32 delay, uint32 emote) pairs.  The client's ``Unpack``
    always consumes all eight records, so a single seeded text is padded with
    seven empty records.
    """
    if text is None:
        return struct.pack("<I", (int(text_id) & 0x7FFFFFFF) | 0x80000000)

    def field(name: str, default: int = 0) -> int:
        try:
            value = text[name]
        except (KeyError, IndexError):
            return default
        return int(value) if value is not None else default

    def real(name: str, default: float = 1.0) -> float:
        try:
            value = text[name]
        except (KeyError, IndexError):
            return default
        return float(value) if value is not None else default

    def string(name: str) -> bytes:
        try:
            value = text[name]
        except (KeyError, IndexError):
            value = ""
        return _cstring(value)

    out = bytearray()
    out += struct.pack("<I", int(text_id) & 0x7FFFFFFF)
    for index in range(8):
        if index == 0:
            out += struct.pack("<f", real("probability", 1.0))
            out += string("text")
            out += string("female_text")
            out += struct.pack("<i", field("language"))
            for emote_index in (1, 2, 3):
                out += struct.pack(
                    "<II",
                    field(f"emote_delay{emote_index}"),
                    field(f"emote{emote_index}"),
                )
        else:
            out += struct.pack("<f", 0.0)
            out += b"\x00\x00"
            out += struct.pack("<i", 0)
            out += struct.pack("<6I", 0, 0, 0, 0, 0, 0)
    return bytes(out)


def build_tutorial_flags(all_seen: bool = True) -> bytes:
    """``SMSG_TUTORIAL_FLAGS``: eight ``uint32`` bitmasks, one bit per tutorial.

    A zeroed mask means "nothing seen yet", so the client opens the
    "Welcome to World of Warcraft!" popup (and every later one) the moment the
    player enters the world.  Marking them all as seen keeps the first login
    clean; a real core persists this per character in ``character_tutorial``.
    """
    return b"\xff" * 32 if all_seen else bytes(32)


# Chat message type constants from the client's ``ChatFrame.h``.  These are the
# values the wire carries, not the slash-command ids; say/yell/whisper/emote
# are the ones we actually relay.
CHAT_MSG_SYSTEM = 0
CHAT_MSG_SAY = 1
CHAT_MSG_PARTY = 2
CHAT_MSG_RAID = 3
CHAT_MSG_YELL = 6
CHAT_MSG_WHISPER = 7
CHAT_MSG_WHISPER_INFORM = 9
CHAT_MSG_EMOTE = 10
CHAT_MSG_TEXT_EMOTE = 11
CHAT_MSG_AFK = 23
CHAT_MSG_DND = 24

# Text-emote ids the client knows (``EmotesRec``).  The first 28 are the ones
# every client build supports: 0 = bow, 1 = beckon, 2 = cower, ... 27 = wave.
TEXT_EMOTE_BOW = 0
TEXT_EMOTE_BECKON = 1
TEXT_EMOTE_SIT = 3
TEXT_EMOTE_STAND = 4
TEXT_EMOTE_KNEEL = 5
TEXT_EMOTE_FLOP = 6
TEXT_EMOTE_SLAP = 7
TEXT_EMOTE_FLEX = 8
TEXT_EMOTE_PAT = 9
TEXT_EMOTE_CRY = 10
TEXT_EMOTE_COWER = 11
TEXT_EMOTE_BEG = 12
TEXT_EMOTE_DANCE = 26
TEXT_EMOTE_WAVE = 27


# Language spell IDs taught by ``playercreateinfo_spell`` in 3.3.5a:
#   668 Common, 669 Orcish, 670 Taurahe, 671 Darnassian, 672 Dwarven,
#   813 Thalassian, 7340 Gnomish, 7341 Troll (Zandali),
#   17737 Gutterspeak, 29932 Draenei.
# The authentic starter table already grants the right ones per race, so
# ``SMSG_INITIAL_SPELLS`` simply sends the character's known spells.


def build_message_chat_payload(message_type: int, language: int, sender_guid: int,
                               sender_name: str, target_guid: int,
                               target_name: str, message: str) -> bytes:
    """``SMSG_MESSAGECHAT`` for a player-to-player message.

    Wire format (ChatFrame.cpp:1588-1643, the ``default`` case in
    ``ChatMessageReceived``): uint8 type, uint32 language, uint64 sender guid,
    uint32 sender unk, uint64 target guid, uint32 msg length, msg bytes + NUL,
    uint8 chat tag.

    There is no target-unk field in the 3.3.5a layout -- ``ChatMessageReceived``
    reads the target guid and then immediately the length.  Adding one shifts
    every later field, so ``IsRead()`` (m_read == m_size) fails and the client
    silently drops the message.  The length includes the trailing NUL, matching
    TrinityCore's ``data->put<uint32>(pos, msg.length() + 1)``.
    """
    text = message.encode("utf-8", errors="replace")
    out = bytearray()
    out += struct.pack("<B", message_type & 0xFF)
    out += struct.pack("<I", int(language) & 0xFFFFFFFF)
    out += struct.pack("<Q", int(sender_guid) & 0xFFFFFFFFFFFFFFFF)
    out += struct.pack("<I", 0)
    out += struct.pack("<Q", int(target_guid) & 0xFFFFFFFFFFFFFFFF)
    out += struct.pack("<I", len(text) + 1)
    out += text
    out += b"\x00"
    out += b"\x00"  # chat tag
    return bytes(out)


def build_text_emote_payload(emote_id: int, guid: int, name: str) -> bytes:
    """``SMSG_TEXT_EMOTE``: uint64 guid, uint32 emote id, uint32 unk, name cstring."""
    out = bytearray()
    out += struct.pack("<Q", int(guid) & 0xFFFFFFFFFFFFFFFF)
    out += struct.pack("<I", int(emote_id) & 0xFFFFFFFF)
    out += struct.pack("<I", 0)
    text = name.encode("utf-8", errors="replace")
    out += struct.pack("<I", len(text) + 1)
    out += text
    out += b"\x00"
    return bytes(out)


def build_emote_payload(emote_id: int, guid: int) -> bytes:
    """``SMSG_EMOTE``: uint32 emote id, uint64 guid."""
    return struct.pack("<IQ", int(emote_id) & 0xFFFFFFFF,
                       int(guid) & 0xFFFFFFFFFFFFFFFF)


def build_client_control_update(char: Mapping, allow_move: bool = True) -> bytes:
    """``SMSG_CLIENT_CONTROL_UPDATE``: packed guid + uint8 allow-move.

    TrinityCore sends this whenever a player gains or loses control of a unit.
    Sending it on login makes the client explicitly mark the character as
    locally controlled instead of relying on its own default.
    """
    return object_update.pack_guid(int(char["guid"])) + bytes([1 if allow_move else 0])


def build_destroy_object(guid: int, death: bool = False) -> bytes:
    """SMSG_DESTROY_OBJECT: the full uint64 guid followed by uint8 death.

    The client removes the object from its object manager and stops rendering
    it; death would play the death animation first, which is not what a
    logout should do.
    """
    return struct.pack("<QB", int(guid), 1 if death else 0)


# Equipment slots the client reads from ``SMSG_MIRRORIMAGE_DATA``, in the order
# its handler adds them (Unit_C.cpp, OnReceiveMirrorImageDataFromServer):
# head, shoulder, shirt/body, chest, waist, legs, feet, wrist, hands, back,
# tabard.  The client's ``AddItemBySlot`` maps them back to its INVSLOT_* ids.
MIRROR_IMAGE_SLOTS = (0, 2, 3, 4, 5, 6, 7, 8, 9, 14, 18)


def build_mirror_image_data(char: Mapping, inventory: Mapping) -> bytes:
    """``SMSG_MIRRORIMAGE_DATA`` (0x402) describing one player's appearance.

    Layout (Unit_C.cpp:23671 ``OnReceiveMirrorImageDataFromServer``):
    uint64 guid, uint32 display id, uint8 race, sex, class, skin, face,
    hair style, hair colour, facial hair, uint32 guild id, then 11 x uint32
    item display ids for ``MIRROR_IMAGE_SLOTS``.

    The client refuses the packet when the display id does not match its own
    ``UNIT_FIELD_DISPLAYID``, so it must be built with the same race/gender
    mapping as the create block (``object_update.player_display_id``).
    """
    out = bytearray()
    out += struct.pack(
        "<QI",
        int(char["guid"]) & 0xFFFFFFFFFFFFFFFF,
        object_update.player_display_id(char),
    )
    out += struct.pack(
        "<8B",
        int(char["race"]) & 0xFF,
        int(char["gender"]) & 0xFF,
        int(char["class"]) & 0xFF,
        int(char["skin"]) & 0xFF,
        int(char["face"]) & 0xFF,
        int(char["hair_style"]) & 0xFF,
        int(char["hair_color"]) & 0xFF,
        int(char["facial_style"]) & 0xFF,
    )
    out += struct.pack("<I", 0)  # guild id (no guilds yet)
    for slot in MIRROR_IMAGE_SLOTS:
        item = inventory.get(slot)
        display_id = 0
        if item is not None:
            keys = item.keys() if hasattr(item, "keys") else ()
            if "display_id" in keys and item["display_id"] is not None:
                display_id = int(item["display_id"])
        out += struct.pack("<I", display_id & 0xFFFFFFFF)
    return bytes(out)


def build_item_push_result(
    player_guid: int,
    item_entry: int,
    bag_slot: int,
    item_slot: int,
    count: int = 1,
    suffix_factor: int = 0,
    random_property_id: int = 0,
    received: int = 0,
    created: int = 0,
    send_chat_message: int = 0,
    total_count: int | None = None,
) -> bytes:
    """Build SMSG_ITEM_PUSH_RESULT for an item appearing in a bag.

    Used to force the client's bag UI to refresh after an item move/swap.
    The packet tells the client an item was "received" in a specific slot.
    ``count`` is the amount added by this event; ``total_count`` is the new
    stack size (defaults to ``count`` for a freshly created stack).
    """
    total = int(count) if total_count is None else int(total_count)
    # Player GUID
    payload = struct.pack("<Q", int(player_guid))
    # received, created, sendChatMessage
    payload += struct.pack("<III", received, created, send_chat_message)
    # bag slot (0 = backpack)
    payload += struct.pack("<B", bag_slot)
    # item slot in bag (0xFFFFFFFF if stacked)
    payload += struct.pack("<i", item_slot)
    # item entry
    payload += struct.pack("<I", item_entry)
    # suffix factor
    payload += struct.pack("<I", suffix_factor)
    # random property id
    payload += struct.pack("<i", random_property_id)
    # count
    payload += struct.pack("<I", count)
    # total count in inventory
    payload += struct.pack("<I", total)
    return payload


def build_login_packets(char: Mapping, motd: str,
                        creature_blocks: List[bytes] | None = None,
                        known_spell_ids: Optional[List[int]] = None,
                        action_buttons: Optional[dict] = None) -> List[Tuple[int, bytes]]:
    """Return ``[(opcode, payload), ...]`` in the order the client expects.

    Order mirrors TrinityCore 3.3.5a ``WorldSession::HandlePlayerLogin`` +
    ``Player::SendInitialPacketsBeforeAddToMap``:

    login verify world -> account data -> feature system status -> MOTD ->
    learned dance moves -> bind point -> initial spells -> action buttons ->
    factions -> time speed -> name query -> tutorial flags -> create objects ->
    client control -> time sync.

    ``SMSG_LOGIN_VERIFY_WORLD`` has to be the first world packet: the client
    creates the local player from it, and every later handler (notably the
    initial-spell handler that fills the spellbook) needs that player object.
    """
    update_opcode, update_payload = object_update.build_update_object(
        char, extra_blocks=creature_blocks
    )
    return [
        (Opcodes.SMSG_LOGIN_VERIFY_WORLD, build_login_verify_world(char)),
        (Opcodes.SMSG_ACCOUNT_DATA_TIMES, build_account_data_times()),
        (Opcodes.SMSG_FEATURE_SYSTEM_STATUS, build_feature_system_status()),
        (Opcodes.SMSG_MOTD, build_motd(motd)),
        (Opcodes.SMSG_LEARNED_DANCE_MOVES, struct.pack("<II", 0, 0)),
        (Opcodes.SMSG_BINDPOINTUPDATE, build_bind_point(char)),
        (Opcodes.SMSG_INITIAL_SPELLS, build_initial_spells(char, known_spell_ids)),
        (Opcodes.SMSG_ACTION_BUTTONS, build_action_buttons(action_buttons)),
        (Opcodes.SMSG_INITIALIZE_FACTIONS, build_initialize_factions()),
        (Opcodes.SMSG_LOGIN_SETTIMESPEED, build_login_set_time_speed()),
        (Opcodes.SMSG_NAME_QUERY_RESPONSE, build_name_query_response(char)),
        (Opcodes.SMSG_TUTORIAL_FLAGS, build_tutorial_flags()),
        (Opcodes.SMSG_INIT_WORLD_STATES, build_init_world_states(char)),
        (update_opcode, update_payload),
        (Opcodes.SMSG_CLIENT_CONTROL_UPDATE, build_client_control_update(char)),
        (Opcodes.SMSG_TIME_SYNC_REQ, struct.pack("<I", 0)),
    ]

"""Static creature (NPC) definitions and spawns for the experimental server.

The tables deliberately mirror only the parts of a TrinityCore world database
that the worldserver needs to put a creature in front of the client:

``creature_templates``
    One row per creature entry: name/title, display model, faction, level and
    the ``UNIT_NPC_FLAGS`` that make the client offer a gossip menu.

``creature_spawns``
    One row per placed creature.  ``guid`` is the low part of the object guid
    the client receives; creatures use no high-guid word in the create block.

``gossip_menus`` / ``gossip_options``
    A very small subset of ``gossip_menu`` / ``gossip_menu_option``.  The menu
    row carries the NPC-text id shown above the option list, and an option can
    either open another menu or replace the current text.

``npc_text``
    The text the client fetches with ``CMSG_NPC_TEXT_QUERY`` after it receives
    ``SMSG_GOSSIP_MESSAGE``.  Only one text variant per id is used; the builder
    expands it to the eight options the client always reads.
"""
from __future__ import annotations

import os
import sqlite3
from typing import List, Optional

from src.world.update_fields import CREATURE_HIGH_GUID


# 3.3.5 creature object guids pack the template entry into bits 24-47:
#   bits 48-63  0xF130 (CREATURE_HIGH_GUID prefix)
#   bits 24-47  creature_template.entry (24 bits)
#   bits  0-23  creature_spawns.guid (24-bit spawn counter)
# The client reads the entry out of the guid to decide which
# ``CMSG_CREATURE_QUERY`` to send.  Without it the client asks about entry 0,
# never receives a name/model, and leaves the creature's nameplate blank until
# the player targets it.
CREATURE_GUID_ENTRY_SHIFT = 24
CREATURE_GUID_ENTRY_MASK = 0xFFFFFF
CREATURE_GUID_COUNTER_MASK = 0xFFFFFF


def creature_guid(low_guid: int, entry: int = 0) -> int:
    """Return the 64-bit object guid for a creature spawn.

    ``low_guid`` is the ``creature_spawns.guid`` spawn counter and ``entry``
    the ``creature_templates.entry`` the client should query.  Callers that have
    no entry pass 0 and the client will not be able to resolve a name, so new
    code should always pass it.
    """
    return (
        CREATURE_HIGH_GUID
        | ((int(entry) & CREATURE_GUID_ENTRY_MASK) << CREATURE_GUID_ENTRY_SHIFT)
        | (int(low_guid) & CREATURE_GUID_COUNTER_MASK)
    )


def creature_low_guid(guid: int) -> int:
    """Return the ``creature_spawns.guid`` stored in a creature object guid."""
    return int(guid) & CREATURE_GUID_COUNTER_MASK


def creature_entry_from_guid(guid: int) -> int:
    """Return the template entry packed into a creature object guid."""
    return (int(guid) >> CREATURE_GUID_ENTRY_SHIFT) & CREATURE_GUID_ENTRY_MASK

CREATE_CREATURE_SQL = """
CREATE TABLE IF NOT EXISTS creature_templates(
  entry INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  subname TEXT NOT NULL DEFAULT '',
  cursor_name TEXT NOT NULL DEFAULT '',
  gossip_menu_id INTEGER NOT NULL DEFAULT 0,
  min_level INTEGER NOT NULL DEFAULT 1,
  max_level INTEGER NOT NULL DEFAULT 1,
  faction INTEGER NOT NULL DEFAULT 1,
  npc_flags INTEGER NOT NULL DEFAULT 0,
  unit_flags INTEGER NOT NULL DEFAULT 2,
  unit_flags2 INTEGER NOT NULL DEFAULT 0,
  unit_class INTEGER NOT NULL DEFAULT 1,
  unit_gender INTEGER NOT NULL DEFAULT 0,
  unit_power_type INTEGER NOT NULL DEFAULT 0,
  scale REAL NOT NULL DEFAULT 1.0,
  type INTEGER NOT NULL DEFAULT 7,
  family INTEGER NOT NULL DEFAULT 0,
  classification INTEGER NOT NULL DEFAULT 0,
  display_id1 INTEGER NOT NULL DEFAULT 0,
  display_id2 INTEGER NOT NULL DEFAULT 0,
  display_id3 INTEGER NOT NULL DEFAULT 0,
  display_id4 INTEGER NOT NULL DEFAULT 0,
  hp_multiplier REAL NOT NULL DEFAULT 1.0,
  energy_multiplier REAL NOT NULL DEFAULT 1.0,
  leader INTEGER NOT NULL DEFAULT 0,
  movement_info_id INTEGER NOT NULL DEFAULT 0,
  -- CMaNGOS ``creature_template.MovementType``: 0 idle, 1 random, 2 waypoint.
  -- -1 means the dump did not provide one, so the runtime uses a heuristic.
  movement_type INTEGER NOT NULL DEFAULT -1,
  bounding_radius REAL NOT NULL DEFAULT 0.389,
  combat_reach REAL NOT NULL DEFAULT 1.5,
  base_health INTEGER NOT NULL DEFAULT 100,
  flags INTEGER NOT NULL DEFAULT 0,
  -- 0 for Blizzard's own tooling content (bunnies, invisible stalkers, target
  -- dummies, "[DND]" test props).  They exist in the world database but are not
  -- creatures a player is meant to see, so the visibility query skips them.
  visible INTEGER NOT NULL DEFAULT 1,
  -- Combat fields
  min_damage REAL NOT NULL DEFAULT 0.0,
  max_damage REAL NOT NULL DEFAULT 0.0,
  attack_power INTEGER NOT NULL DEFAULT 0,
  ranged_attack_power INTEGER NOT NULL DEFAULT 0,
  armor INTEGER NOT NULL DEFAULT 0,
  melee_attack_speed INTEGER NOT NULL DEFAULT 2000,
  ranged_attack_speed INTEGER NOT NULL DEFAULT 3000,
  crit_chance REAL NOT NULL DEFAULT 0.0,
  -- AI
  ai_name TEXT NOT NULL DEFAULT '',
  aggro_radius REAL NOT NULL DEFAULT 20.0,
  chase_distance REAL NOT NULL DEFAULT 50.0,
  -- Movement
  speed_walk REAL NOT NULL DEFAULT 2.5,
  speed_run REAL NOT NULL DEFAULT 8.0,
  speed_swim REAL NOT NULL DEFAULT 4.722222,
  -- Respawn
  respawn_time INTEGER NOT NULL DEFAULT 300,
  -- Kill credit
  kill_credit1 INTEGER NOT NULL DEFAULT 0,
  kill_credit2 INTEGER NOT NULL DEFAULT 0,
  -- Quest items
  quest_item1 INTEGER NOT NULL DEFAULT 0,
  quest_item2 INTEGER NOT NULL DEFAULT 0,
  quest_item3 INTEGER NOT NULL DEFAULT 0,
  quest_item4 INTEGER NOT NULL DEFAULT 0,
  quest_item5 INTEGER NOT NULL DEFAULT 0,
  quest_item6 INTEGER NOT NULL DEFAULT 0,
  -- Loot: CMaNGOS creature_template.LootId.  Usually equal to Entry, but Naxx
  -- and a few event NPCs share a loot pool, so the loot template has to be
  -- looked up by this id when it is set.
  loot_id INTEGER NOT NULL DEFAULT 0,
  -- Display
  display_id INTEGER NOT NULL DEFAULT 0,
  -- Spell list
  spell_list_id INTEGER NOT NULL DEFAULT 0,
  -- Model info
  model_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS creature_spawns(
  guid INTEGER PRIMARY KEY,
  entry INTEGER NOT NULL REFERENCES creature_templates(entry),
  map INTEGER NOT NULL DEFAULT 0,
  zone INTEGER NOT NULL DEFAULT 0,
  x REAL NOT NULL DEFAULT 0.0,
  y REAL NOT NULL DEFAULT 0.0,
  z REAL NOT NULL DEFAULT 0.0,
  o REAL NOT NULL DEFAULT 0.0,
  display_id INTEGER NOT NULL DEFAULT 0,
  spawn_time INTEGER NOT NULL DEFAULT 0,
  -- Combat state
  current_health INTEGER NOT NULL DEFAULT 0,
  current_power INTEGER NOT NULL DEFAULT 0,
  combat_state INTEGER NOT NULL DEFAULT 0,  -- 0=peace, 1=combat, 2=evade, 3=flee
  target_guid INTEGER NOT NULL DEFAULT 0,
  home_x REAL NOT NULL DEFAULT 0.0,
  home_y REAL NOT NULL DEFAULT 0.0,
  home_z REAL NOT NULL DEFAULT 0.0,
  home_o REAL NOT NULL DEFAULT 0.0,
  -- Movement (CMaNGOS ``creature.spawndist`` / ``creature.MovementType``).
  -- ``movement_type`` is -1 when the world dump did not provide one, which
  -- lets the server fall back to the template's MovementType or a heuristic.
  -- 0 = idle, 1 = random wander, 2 = waypoint path.
  movement_type INTEGER NOT NULL DEFAULT -1,
  spawndist REAL NOT NULL DEFAULT 0.0,
  -- Respawn
  dead_since INTEGER NOT NULL DEFAULT 0,
  -- Loot (JSON: {"items": [[item_entry, count], ...], "money": copper})
  loot_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_creature_spawns_map ON creature_spawns(map);
-- Visibility queries bound a square around the player before doing the exact
-- distance check, so this composite index keeps login scans cheap even with a
-- full 3.3.5 spawn table (~100k rows).
CREATE INDEX IF NOT EXISTS idx_creature_spawns_map_xy ON creature_spawns(map, x, y);

-- Per-spawn waypoint paths (CMaNGOS ``creature_movement``).  ``Id`` is the
-- creature's spawn GUID, ``Point`` orders the path, ``WaitTime`` is the delay in
-- milliseconds before moving on, and ``ScriptId`` is unused by this server.
CREATE TABLE IF NOT EXISTS creature_movement(
  id INTEGER NOT NULL,
  point INTEGER NOT NULL,
  position_x REAL NOT NULL DEFAULT 0.0,
  position_y REAL NOT NULL DEFAULT 0.0,
  position_z REAL NOT NULL DEFAULT 0.0,
  orientation REAL NOT NULL DEFAULT 0.0,
  wait_time INTEGER NOT NULL DEFAULT 0,
  script_id INTEGER NOT NULL DEFAULT 0,
  comment TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(id, point)
);

-- Shared waypoint paths (CMaNGOS ``creature_movement_template``).  Keyed by the
-- creature entry and a path id (``creature_template.MovementTemplateId``).
CREATE TABLE IF NOT EXISTS creature_movement_template(
  entry INTEGER NOT NULL,
  path_id INTEGER NOT NULL DEFAULT 0,
  point INTEGER NOT NULL,
  position_x REAL NOT NULL DEFAULT 0.0,
  position_y REAL NOT NULL DEFAULT 0.0,
  position_z REAL NOT NULL DEFAULT 0.0,
  orientation REAL NOT NULL DEFAULT 0.0,
  wait_time INTEGER NOT NULL DEFAULT 0,
  script_id INTEGER NOT NULL DEFAULT 0,
  comment TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(entry, path_id, point)
);

CREATE TABLE IF NOT EXISTS gossip_menus(
  menu_id INTEGER PRIMARY KEY,
  text_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gossip_options(
  menu_id INTEGER NOT NULL,
  option_index INTEGER NOT NULL,
  option_type INTEGER NOT NULL DEFAULT 0,
  icon INTEGER NOT NULL DEFAULT 0,
  coded INTEGER NOT NULL DEFAULT 0,
  box_money INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT '',
  box_text TEXT NOT NULL DEFAULT '',
  action_menu_id INTEGER NOT NULL DEFAULT 0,
  action_text_id INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(menu_id, option_index)
);

CREATE TABLE IF NOT EXISTS npc_text(
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL DEFAULT '',
  female_text TEXT NOT NULL DEFAULT '',
  language INTEGER NOT NULL DEFAULT 0,
  probability REAL NOT NULL DEFAULT 1.0,
  emote1 INTEGER NOT NULL DEFAULT 0,
  emote_delay1 INTEGER NOT NULL DEFAULT 0,
  emote2 INTEGER NOT NULL DEFAULT 0,
  emote_delay2 INTEGER NOT NULL DEFAULT 0,
  emote3 INTEGER NOT NULL DEFAULT 0,
  emote_delay3 INTEGER NOT NULL DEFAULT 0
);
-- Spell system tables (append to CREATE_CREATURE_SQL)

-- Spell system tables
CREATE TABLE IF NOT EXISTS spell_templates(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  rank TEXT NOT NULL DEFAULT '',
  school INTEGER NOT NULL DEFAULT 0,
  category INTEGER NOT NULL DEFAULT 0,
  dispel_type INTEGER NOT NULL DEFAULT 0,
  mechanic INTEGER NOT NULL DEFAULT 0,
  attributes INTEGER NOT NULL DEFAULT 0,
  attributes_ex INTEGER NOT NULL DEFAULT 0,
  attributes_ex2 INTEGER NOT NULL DEFAULT 0,
  attributes_ex3 INTEGER NOT NULL DEFAULT 0,
  attributes_ex4 INTEGER NOT NULL DEFAULT 0,
  attributes_ex5 INTEGER NOT NULL DEFAULT 0,
  attributes_ex6 INTEGER NOT NULL DEFAULT 0,
  attributes_ex7 INTEGER NOT NULL DEFAULT 0,
  stances INTEGER NOT NULL DEFAULT 0,
  stances_not INTEGER NOT NULL DEFAULT 0,
  targets INTEGER NOT NULL DEFAULT 0,
  target_creature_type INTEGER NOT NULL DEFAULT 0,
  requires_spell_focus INTEGER NOT NULL DEFAULT 0,
  caster_aura_state INTEGER NOT NULL DEFAULT 0,
  target_aura_state INTEGER NOT NULL DEFAULT 0,
  caster_aura_state_not INTEGER NOT NULL DEFAULT 0,
  target_aura_state_not INTEGER NOT NULL DEFAULT 0,
  caster_aura_spell INTEGER NOT NULL DEFAULT 0,
  target_aura_spell INTEGER NOT NULL DEFAULT 0,
  exclude_caster_aura_spell INTEGER NOT NULL DEFAULT 0,
  exclude_target_aura_spell INTEGER NOT NULL DEFAULT 0,
  casting_time_index INTEGER NOT NULL DEFAULT 0,
  recovery_time INTEGER NOT NULL DEFAULT 0,
  category_recovery_time INTEGER NOT NULL DEFAULT 0,
  interrupt_flags INTEGER NOT NULL DEFAULT 0,
  aura_interrupt_flags INTEGER NOT NULL DEFAULT 0,
  channel_interrupt_flags INTEGER NOT NULL DEFAULT 0,
  proc_flags INTEGER NOT NULL DEFAULT 0,
  proc_chance INTEGER NOT NULL DEFAULT 0,
  proc_charges INTEGER NOT NULL DEFAULT 0,
  max_level INTEGER NOT NULL DEFAULT 0,
  base_level INTEGER NOT NULL DEFAULT 0,
  spell_level INTEGER NOT NULL DEFAULT 0,
  duration_index INTEGER NOT NULL DEFAULT 0,
  power_type INTEGER NOT NULL DEFAULT 0,
  mana_cost INTEGER NOT NULL DEFAULT 0,
  mana_cost_per_level INTEGER NOT NULL DEFAULT 0,
  mana_per_second INTEGER NOT NULL DEFAULT 0,
  mana_per_second_per_level INTEGER NOT NULL DEFAULT 0,
  range_index INTEGER NOT NULL DEFAULT 0,
  speed REAL NOT NULL DEFAULT 0.0,
  modal_next_spell INTEGER NOT NULL DEFAULT 0,
  stack_amount INTEGER NOT NULL DEFAULT 0,
  totem1 INTEGER NOT NULL DEFAULT 0,
  totem2 INTEGER NOT NULL DEFAULT 0,
  reagent1 INTEGER NOT NULL DEFAULT 0,
  reagent2 INTEGER NOT NULL DEFAULT 0,
  reagent3 INTEGER NOT NULL DEFAULT 0,
  reagent4 INTEGER NOT NULL DEFAULT 0,
  reagent5 INTEGER NOT NULL DEFAULT 0,
  reagent6 INTEGER NOT NULL DEFAULT 0,
  reagent7 INTEGER NOT NULL DEFAULT 0,
  reagent8 INTEGER NOT NULL DEFAULT 0,
  reagent_count1 INTEGER NOT NULL DEFAULT 0,
  reagent_count2 INTEGER NOT NULL DEFAULT 0,
  reagent_count3 INTEGER NOT NULL DEFAULT 0,
  reagent_count4 INTEGER NOT NULL DEFAULT 0,
  reagent_count5 INTEGER NOT NULL DEFAULT 0,
  reagent_count6 INTEGER NOT NULL DEFAULT 0,
  reagent_count7 INTEGER NOT NULL DEFAULT 0,
  reagent_count8 INTEGER NOT NULL DEFAULT 0,
  equipped_item_class INTEGER NOT NULL DEFAULT 0,
  equipped_item_subclass_mask INTEGER NOT NULL DEFAULT 0,
  equipped_item_inventory_type_mask INTEGER NOT NULL DEFAULT 0,
  effect1 INTEGER NOT NULL DEFAULT 0,
  effect2 INTEGER NOT NULL DEFAULT 0,
  effect3 INTEGER NOT NULL DEFAULT 0,
  effect_die_sides1 INTEGER NOT NULL DEFAULT 0,
  effect_die_sides2 INTEGER NOT NULL DEFAULT 0,
  effect_die_sides3 INTEGER NOT NULL DEFAULT 0,
  effect_real_points_per_level1 REAL NOT NULL DEFAULT 0.0,
  effect_real_points_per_level2 REAL NOT NULL DEFAULT 0.0,
  effect_real_points_per_level3 REAL NOT NULL DEFAULT 0.0,
  effect_base_points1 INTEGER NOT NULL DEFAULT 0,
  effect_base_points2 INTEGER NOT NULL DEFAULT 0,
  effect_base_points3 INTEGER NOT NULL DEFAULT 0,
  effect_mechanic1 INTEGER NOT NULL DEFAULT 0,
  effect_mechanic2 INTEGER NOT NULL DEFAULT 0,
  effect_mechanic3 INTEGER NOT NULL DEFAULT 0,
  effect_implicit_target_a1 INTEGER NOT NULL DEFAULT 0,
  effect_implicit_target_a2 INTEGER NOT NULL DEFAULT 0,
  effect_implicit_target_a3 INTEGER NOT NULL DEFAULT 0,
  effect_implicit_target_b1 INTEGER NOT NULL DEFAULT 0,
  effect_implicit_target_b2 INTEGER NOT NULL DEFAULT 0,
  effect_implicit_target_b3 INTEGER NOT NULL DEFAULT 0,
  effect_radius_index1 INTEGER NOT NULL DEFAULT 0,
  effect_radius_index2 INTEGER NOT NULL DEFAULT 0,
  effect_radius_index3 INTEGER NOT NULL DEFAULT 0,
  effect_apply_aura_name1 INTEGER NOT NULL DEFAULT 0,
  effect_apply_aura_name2 INTEGER NOT NULL DEFAULT 0,
  effect_apply_aura_name3 INTEGER NOT NULL DEFAULT 0,
  effect_amplitude1 INTEGER NOT NULL DEFAULT 0,
  effect_amplitude2 INTEGER NOT NULL DEFAULT 0,
  effect_amplitude3 INTEGER NOT NULL DEFAULT 0,
  effect_multiple_value1 REAL NOT NULL DEFAULT 0.0,
  effect_multiple_value2 REAL NOT NULL DEFAULT 0.0,
  effect_multiple_value3 REAL NOT NULL DEFAULT 0.0,
  effect_chain_target1 INTEGER NOT NULL DEFAULT 0,
  effect_chain_target2 INTEGER NOT NULL DEFAULT 0,
  effect_chain_target3 INTEGER NOT NULL DEFAULT 0,
  effect_item_type1 INTEGER NOT NULL DEFAULT 0,
  effect_item_type2 INTEGER NOT NULL DEFAULT 0,
  effect_item_type3 INTEGER NOT NULL DEFAULT 0,
  effect_misc_value1 INTEGER NOT NULL DEFAULT 0,
  effect_misc_value2 INTEGER NOT NULL DEFAULT 0,
  effect_misc_value3 INTEGER NOT NULL DEFAULT 0,
  effect_misc_value_b1 INTEGER NOT NULL DEFAULT 0,
  effect_misc_value_b2 INTEGER NOT NULL DEFAULT 0,
  effect_misc_value_b3 INTEGER NOT NULL DEFAULT 0,
  effect_trigger_spell1 INTEGER NOT NULL DEFAULT 0,
  effect_trigger_spell2 INTEGER NOT NULL DEFAULT 0,
  effect_trigger_spell3 INTEGER NOT NULL DEFAULT 0,
  effect_points_per_combo_point1 REAL NOT NULL DEFAULT 0.0,
  effect_points_per_combo_point2 REAL NOT NULL DEFAULT 0.0,
  effect_points_per_combo_point3 REAL NOT NULL DEFAULT 0.0,
  effect_spell_class_mask1_1 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask1_2 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask1_3 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask2_1 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask2_2 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask2_3 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask3_1 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask3_2 INTEGER NOT NULL DEFAULT 0,
  effect_spell_class_mask3_3 INTEGER NOT NULL DEFAULT 0,
  spell_visual INTEGER NOT NULL DEFAULT 0,
  spell_visual2 INTEGER NOT NULL DEFAULT 0,
  spell_icon_id INTEGER NOT NULL DEFAULT 0,
  active_icon_id INTEGER NOT NULL DEFAULT 0,
  spell_priority INTEGER NOT NULL DEFAULT 0,
  spell_name TEXT NOT NULL DEFAULT '',
  rank1 TEXT NOT NULL DEFAULT '',
  rank2 TEXT NOT NULL DEFAULT '',
  rank3 TEXT NOT NULL DEFAULT '',
  rank4 TEXT NOT NULL DEFAULT '',
  rank5 TEXT NOT NULL DEFAULT '',
  rank6 TEXT NOT NULL DEFAULT '',
  rank7 TEXT NOT NULL DEFAULT '',
  rank8 TEXT NOT NULL DEFAULT '',
  rank9 TEXT NOT NULL DEFAULT '',
  rank10 TEXT NOT NULL DEFAULT '',
  rank11 TEXT NOT NULL DEFAULT '',
  rank12 TEXT NOT NULL DEFAULT '',
  rank13 TEXT NOT NULL DEFAULT '',
  rank14 TEXT NOT NULL DEFAULT '',
  rank15 TEXT NOT NULL DEFAULT '',
  rank16 TEXT NOT NULL DEFAULT '',
  mana_cost_percentage INTEGER NOT NULL DEFAULT 0,
  start_recovery_category INTEGER NOT NULL DEFAULT 0,
  start_recovery_time INTEGER NOT NULL DEFAULT 0,
  max_target_level INTEGER NOT NULL DEFAULT 0,
  spell_family_name INTEGER NOT NULL DEFAULT 0,
  spell_family_flags INTEGER NOT NULL DEFAULT 0,
  spell_family_flags2 INTEGER NOT NULL DEFAULT 0,
  max_affected_targets INTEGER NOT NULL DEFAULT 0,
  dmg_class INTEGER NOT NULL DEFAULT 0,
  prevention_type INTEGER NOT NULL DEFAULT 0,
  stance_bar_order INTEGER NOT NULL DEFAULT 0,
  dmg_multiplier1 REAL NOT NULL DEFAULT 1.0,
  dmg_multiplier2 REAL NOT NULL DEFAULT 1.0,
  dmg_multiplier3 REAL NOT NULL DEFAULT 1.0,
  min_faction_id INTEGER NOT NULL DEFAULT 0,
  min_reputation INTEGER NOT NULL DEFAULT 0,
  required_aura_vision INTEGER NOT NULL DEFAULT 0,
  totem_category1 INTEGER NOT NULL DEFAULT 0,
  totem_category2 INTEGER NOT NULL DEFAULT 0,
  area_id INTEGER NOT NULL DEFAULT 0,
  school_mask INTEGER NOT NULL DEFAULT 0,
  rune_cost_id INTEGER NOT NULL DEFAULT 0,
  spell_missile_id INTEGER NOT NULL DEFAULT 0,
  power_display_id INTEGER NOT NULL DEFAULT 0,
  effect_bonus_coefficient1 REAL NOT NULL DEFAULT 0.0,
  effect_bonus_coefficient2 REAL NOT NULL DEFAULT 0.0,
  effect_bonus_coefficient3 REAL NOT NULL DEFAULT 0.0,
  effect_bonus_coefficient_from_ap1 REAL NOT NULL DEFAULT 0.0,
  effect_bonus_coefficient_from_ap2 REAL NOT NULL DEFAULT 0.0,
  effect_bonus_coefficient_from_ap3 REAL NOT NULL DEFAULT 0.0,
  spell_description_variable_id INTEGER NOT NULL DEFAULT 0,
  spell_difficulty_id INTEGER NOT NULL DEFAULT 0,
  is_server_side INTEGER NOT NULL DEFAULT 0,
  attributes_serverside INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS creature_template_spells(
  entry INTEGER NOT NULL DEFAULT 0,
  set_id INTEGER NOT NULL DEFAULT 0,
  spell1 INTEGER NOT NULL DEFAULT 0,
  spell2 INTEGER NOT NULL DEFAULT 0,
  spell3 INTEGER NOT NULL DEFAULT 0,
  spell4 INTEGER NOT NULL DEFAULT 0,
  spell5 INTEGER NOT NULL DEFAULT 0,
  spell6 INTEGER NOT NULL DEFAULT 0,
  spell7 INTEGER NOT NULL DEFAULT 0,
  spell8 INTEGER NOT NULL DEFAULT 0,
  spell9 INTEGER NOT NULL DEFAULT 0,
  spell10 INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(entry, set_id)
);

CREATE TABLE IF NOT EXISTS creature_spell_list(
  id INTEGER NOT NULL DEFAULT 0,
  position INTEGER NOT NULL DEFAULT 0,
  spell_id INTEGER NOT NULL DEFAULT 0,
  flags INTEGER NOT NULL DEFAULT 0,
  combat_condition INTEGER NOT NULL DEFAULT -1,
  target_id INTEGER NOT NULL DEFAULT 0,
  script_id INTEGER NOT NULL DEFAULT 0,
  availability INTEGER NOT NULL DEFAULT 0,
  probability INTEGER NOT NULL DEFAULT 0,
  initial_min INTEGER NOT NULL DEFAULT 0,
  initial_max INTEGER NOT NULL DEFAULT 0,
  repeat_min INTEGER NOT NULL DEFAULT 0,
  repeat_max INTEGER NOT NULL DEFAULT 0,
  comments TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(id, position)
);

-- Per-list settings (CMaNGOS creature_spell_list_entry).  The chances decide
-- how often the AI picks a support (heal/buff) or ranged action each tick.
CREATE TABLE IF NOT EXISTS creature_spell_list_entry(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  chance_support_action INTEGER NOT NULL DEFAULT 0,
  chance_ranged_attack INTEGER NOT NULL DEFAULT 0
);

-- Target selectors referenced by creature_spell_list.target_id.  Ids below 100
-- are hardcoded (none/current/self/friendly dispel/friendly missing buff);
-- Type 1 is an attack target, Type 2 a support (heal) target.
CREATE TABLE IF NOT EXISTS creature_spell_targeting(
  id INTEGER PRIMARY KEY,
  type INTEGER NOT NULL DEFAULT 0,
  param1 INTEGER NOT NULL DEFAULT 0,
  param2 INTEGER NOT NULL DEFAULT 0,
  param3 INTEGER NOT NULL DEFAULT 0,
  unit_condition INTEGER NOT NULL DEFAULT -1,
  comments TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS creature_model_info(
  modelid INTEGER PRIMARY KEY,
  bounding_radius REAL NOT NULL DEFAULT 0.0,
  combat_reach REAL NOT NULL DEFAULT 0.0,
  speed_walk REAL NOT NULL DEFAULT 1.0,
  speed_run REAL NOT NULL DEFAULT 1.0,
  gender INTEGER NOT NULL DEFAULT 0,
  modelid_other_gender INTEGER NOT NULL DEFAULT 0,
  modelid_alternative INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS creature_template_classlevelstats(
  level INTEGER NOT NULL,
  class INTEGER NOT NULL,
  base_health_exp0 INTEGER NOT NULL DEFAULT 0,
  base_health_exp1 INTEGER NOT NULL DEFAULT 0,
  base_health_exp2 INTEGER NOT NULL DEFAULT 0,
  base_mana INTEGER NOT NULL DEFAULT 0,
  base_damage_exp0 REAL NOT NULL DEFAULT 0.0,
  base_damage_exp1 REAL NOT NULL DEFAULT 0.0,
  base_damage_exp2 REAL NOT NULL DEFAULT 0.0,
  base_melee_attack_power INTEGER NOT NULL DEFAULT 0,
  base_ranged_attack_power INTEGER NOT NULL DEFAULT 0,
  base_armor INTEGER NOT NULL DEFAULT 0,
  strength INTEGER NOT NULL DEFAULT 0,
  agility INTEGER NOT NULL DEFAULT 0,
  stamina INTEGER NOT NULL DEFAULT 0,
  intellect INTEGER NOT NULL DEFAULT 0,
  spirit INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(level, class)
);

CREATE TABLE IF NOT EXISTS creature_loot_template(
  entry INTEGER NOT NULL,
  item INTEGER NOT NULL DEFAULT 0,
  chance_or_quest_chance REAL NOT NULL DEFAULT 0.0,
  group_id INTEGER NOT NULL DEFAULT 0,
  mincount_or_ref INTEGER NOT NULL DEFAULT 1,
  maxcount INTEGER NOT NULL DEFAULT 1,
  condition_id INTEGER NOT NULL DEFAULT 0,
  comments TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(entry, item, group_id)
);

-- Shared loot pools.  A creature_loot_template row with a negative
-- mincount_or_ref points at the reference entry with that absolute value;
-- every row here is then rolled as a nested loot template.
CREATE TABLE IF NOT EXISTS reference_loot_template(
  entry INTEGER NOT NULL,
  item INTEGER NOT NULL DEFAULT 0,
  chance_or_quest_chance REAL NOT NULL DEFAULT 0.0,
  group_id INTEGER NOT NULL DEFAULT 0,
  mincount_or_ref INTEGER NOT NULL DEFAULT 1,
  maxcount INTEGER NOT NULL DEFAULT 1,
  condition_id INTEGER NOT NULL DEFAULT 0,
  comments TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(entry, item, group_id)
);

CREATE TABLE IF NOT EXISTS creature_equip_template(
  entry INTEGER PRIMARY KEY,
  equipentry1 INTEGER NOT NULL DEFAULT 0,
  equipentry2 INTEGER NOT NULL DEFAULT 0,
  equipentry3 INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS creature_immunities(
  entry INTEGER NOT NULL,
  set_id INTEGER NOT NULL DEFAULT 0,
  type INTEGER NOT NULL DEFAULT 0,
  value INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(entry, set_id, type)
);

CREATE TABLE IF NOT EXISTS creature_cooldowns(
  entry INTEGER NOT NULL,
  spell_id INTEGER NOT NULL DEFAULT 0,
  cooldown_min INTEGER NOT NULL DEFAULT 0,
  cooldown_max INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(entry, spell_id)
);

CREATE TABLE IF NOT EXISTS spell_cone(
  id INTEGER PRIMARY KEY,
  cone_degrees REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS spell_target_position(
  id INTEGER PRIMARY KEY,
  target_map INTEGER NOT NULL DEFAULT 0,
  target_position_x REAL NOT NULL DEFAULT 0.0,
  target_position_y REAL NOT NULL DEFAULT 0.0,
  target_position_z REAL NOT NULL DEFAULT 0.0,
  target_orientation REAL NOT NULL DEFAULT 0.0
);

-- SpellRuneCost.dbc: how many blood/unholy/frost runes a death knight spell
-- consumes and how much runic power it grants.
CREATE TABLE IF NOT EXISTS spell_rune_cost(
  id INTEGER PRIMARY KEY,
  blood INTEGER NOT NULL DEFAULT 0,
  unholy INTEGER NOT NULL DEFAULT 0,
  frost INTEGER NOT NULL DEFAULT 0,
  runic_power INTEGER NOT NULL DEFAULT 0
);

-- SpellDuration.dbc: how long a spell's aura/summon lasts.  Duration values are
-- milliseconds; a negative base means "until cancelled" (warlock pets use -1).
CREATE TABLE IF NOT EXISTS spell_duration(
  id INTEGER PRIMARY KEY,
  duration INTEGER NOT NULL DEFAULT 0,
  duration_per_level INTEGER NOT NULL DEFAULT 0,
  max_duration INTEGER NOT NULL DEFAULT 0
);

-- CMaNGOS ``playercreateinfo_spell``: the exact spells every race/class
-- combination knows at creation, including racials, languages, armor and
-- weapon proficiencies.  Seeded from playercreateinfo.sql on first open.
CREATE TABLE IF NOT EXISTS playercreateinfo_spell(
  race INTEGER NOT NULL DEFAULT 0,
  class INTEGER NOT NULL DEFAULT 0,
  spell INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(race, class, spell)
);

-- CMaNGOS ``playercreateinfo_action``: the default action bar per race/class.
-- ``type`` is 0 for a spell and 128 for a macro button.
CREATE TABLE IF NOT EXISTS playercreateinfo_action(
  race INTEGER NOT NULL DEFAULT 0,
  class INTEGER NOT NULL DEFAULT 0,
  button INTEGER NOT NULL DEFAULT 0,
  action INTEGER NOT NULL DEFAULT 0,
  type INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(race, class, button)
);

"""

# The demo population of Northshire Valley (map 0, zone 12).  Display ids are
# taken from the client's CreatureDisplayInfo.dbc, so every model definitely
# exists in a clean 3.3.5a install.  The four humanoids stand around the Abbey;
# the beasts/critters are scattered in the vineyard.
_CREATURE_TEMPLATES = [
    # entry, name, subname, gossip_menu, level, faction, npc_flags, type,
    # family, display, scale, health, gender, unit_flags
    (197, "Marshal McBride", "Marshal", 1, 5, 1, 0x00000001, 7, 0, 1276, 1.0, 150, 0, 0x2),
    (823, "Deputy Willem", "Deputy", 2, 5, 1, 0x00000001, 7, 0, 1288, 1.0, 150, 0, 0x2),
    (295, "Innkeeper Farley", "Innkeeper", 3, 5, 1, 0x00010001, 7, 0, 1290, 1.0, 150, 0, 0x2),
    (2327, "Michelle Belle", "Trade Goods", 4, 5, 1, 0x00000001, 7, 0, 176, 1.0, 150, 1, 0x2),
    (299, "Northshire Wolf", "", 0, 3, 1, 0, 1, 1, 161, 0.9, 60, 0, 0x2),
    (193, "Sheep", "", 0, 1, 1, 0, 8, 0, 856, 0.6, 10, 0, 0x2),
    (6, "Kobold Vermin", "", 0, 2, 1, 0, 7, 0, 26, 0.8, 40, 0, 0x2),
    (2442, "Cow", "", 0, 1, 1, 0, 8, 0, 1060, 0.8, 10, 0, 0x2),
    (620, "Chicken", "", 0, 1, 1, 0, 8, 0, 304, 0.4, 5, 0, 0x2),
    (113, "Boar", "", 0, 3, 1, 0, 1, 5, 193, 0.8, 60, 0, 0x2),
    (228, "Crab", "", 0, 2, 1, 0, 1, 8, 342, 0.5, 30, 0, 0x2),
]

# guid, entry, map, zone, x, y, z, o
# The four humanoids face the human starting point (-8949.95, -132.49) so the
# player sees their fronts when entering the world.  Critters keep arbitrary
# headings; animals do not need to face the road.
_CREATURE_SPAWNS = [
    (1001, 197, 0, 12, -8925.0, -130.0, 83.5, 3.2411),
    (1002, 823, 0, 12, -8935.0, -145.0, 83.5, 2.4448),
    (1003, 295, 0, 12, -8910.0, -145.0, 83.5, 2.8381),
    (1004, 2327, 0, 12, -8910.0, -115.0, 83.5, 3.5543),
    (1005, 299, 0, 12, -8970.0, -150.0, 83.0, 2.0),
    (1006, 193, 0, 12, -8960.0, -110.0, 83.0, 4.0),
    (1007, 6, 0, 12, -8985.0, -135.0, 83.0, 1.5),
    (1008, 2442, 0, 12, -8940.0, -170.0, 83.0, 0.5),
    (1009, 620, 0, 12, -8955.0, -95.0, 83.0, 1.0),
    (1010, 113, 0, 12, -8990.0, -160.0, 83.0, 5.0),
    (1011, 228, 0, 12, -8980.0, -105.0, 82.0, 2.5),
]

_GOSSIP_MENUS = [
    (1, 1001),
    (2, 1003),
    (3, 1004),
    (4, 1005),
]

# menu_id, index, type, icon, coded, money, text, box_text, action_menu, action_text
_GOSSIP_OPTIONS = [
    (1, 0, 0, 0, 0, 0, "Where can I find work?", "", 0, 1002),
    (1, 1, 0, 0, 0, 0, "Goodbye.", "", 0, 0),
    (2, 0, 0, 0, 0, 0, "What can you tell me about the kobolds?", "", 0, 1006),
    (2, 1, 0, 0, 0, 0, "Goodbye.", "", 0, 0),
    (3, 0, 0, 0, 0, 0, "I'd like to rest for a while.", "", 0, 1007),
    (3, 1, 0, 0, 0, 0, "Goodbye.", "", 0, 0),
    (4, 0, 0, 0, 0, 0, "Goodbye.", "", 0, 0),
]

_NPC_TEXT = [
    (1001, "Welcome to Northshire. The Abbey is always in need of able-bodied adventurers."),
    (1002, "Deputy Willem is organising the defence. Speak with him near the Abbey gate."),
    (1003, "Kobolds have been stealing our tools again. Keep an eye out for them in the vineyards."),
    (1004, "Rest here as long as you like. You look like you've walked a long way."),
    (1005, "I have goods for sale, but the market stalls are still being set up."),
    (1006, "They burrow in the hills east of the Abbey. Small, cowardly, and fond of candles."),
    (1007, "Sleep well. The Abbey bells will wake you at dawn."),
]


def _schema_columns(sql: str, table: str):
    """Return ``[(column, declaration), ...]`` for ``table`` inside ``sql``.

    ``init_creature_db`` uses this to upgrade databases created by older builds.
    Listing the extra columns by hand went stale once (``unit_flags2`` and
    friends were missing), which made every spawn query fail on an upgraded
    database, so the ALTER list is derived from ``CREATE_CREATURE_SQL`` instead.
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table}("
    clean = "\n".join(line.split("--")[0] for line in sql.splitlines())
    start = clean.index(marker) + len(marker)
    depth, pos = 1, start
    while depth and pos < len(clean):
        if clean[pos] == "(":
            depth += 1
        elif clean[pos] == ")":
            depth -= 1
        pos += 1
    body = clean[start:pos - 1]

    parts, current, depth = [], "", 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())

    columns = []
    for part in parts:
        tokens = part.split(None, 1)
        if not tokens:
            continue
        name = tokens[0].strip("`")
        if name.upper() in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
            continue
        # ALTER TABLE cannot add a primary key, and entry always exists.
        declaration = (tokens[1] if len(tokens) > 1 else "").replace("PRIMARY KEY", "")
        columns.append((name, declaration.strip()))
    return columns


_TEMPLATE_COLUMNS = _schema_columns(CREATE_CREATURE_SQL, "creature_templates")
_SPAWN_COLUMNS = _schema_columns(CREATE_CREATURE_SQL, "creature_spawns")


_PLAYERCREATEINFO_SQL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "playercreateinfo.sql")


def _seed_playercreateinfo(con: sqlite3.Connection) -> None:
    """Load the bundled 3.3.5a starter spell/action tables once.

    ``playercreateinfo.sql`` is generated from the CMaNGOS WoTLKDB dump, so a
    fresh database starts with the exact level-1 spellbook and action bar of
    every race/class combination even before the world dump is imported.
    """
    count = con.execute(
        "SELECT COUNT(*) FROM playercreateinfo_spell").fetchone()[0]
    if count:
        return
    if not os.path.exists(_PLAYERCREATEINFO_SQL):
        return
    with open(_PLAYERCREATEINFO_SQL, encoding="utf-8") as handle:
        con.executescript(handle.read())
    con.commit()


def init_creature_db(path: str) -> None:
    """Create/upgrade the creature tables.

    The eleven Northshire NPCs are a demo population for a fresh database.  Once
    a real world dump has been imported the spawn table is not empty, so seeding
    is skipped and re-running ``init_db.py`` never resurrects the demo rows.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(CREATE_CREATURE_SQL)
        # Migrate creature_templates
        columns = {row[1] for row in con.execute("PRAGMA table_info(creature_templates)")}
        for column, declaration in _TEMPLATE_COLUMNS:
            if column not in columns:
                con.execute(
                    f"ALTER TABLE creature_templates ADD COLUMN {column} {declaration}"
                )
        # Migrate creature_spawns
        spawn_columns = {row[1] for row in con.execute("PRAGMA table_info(creature_spawns)")}
        for column, declaration in _SPAWN_COLUMNS:
            if column not in spawn_columns:
                con.execute(
                    f"ALTER TABLE creature_spawns ADD COLUMN {column} {declaration}"
                )
        # Level-1 spells and action buttons have no connection to the spawn
        # table, so seed them before the demo-population early return.
        _seed_playercreateinfo(con)
        if con.execute("SELECT COUNT(*) FROM creature_spawns").fetchone()[0]:
            con.commit()
            return
        for (entry, name, subname, menu, level, faction, npc_flags, ctype,
             family, display, scale, health, gender, unit_flags) in _CREATURE_TEMPLATES:
            con.execute(
                "INSERT OR IGNORE INTO creature_templates("
                "entry, name, subname, gossip_menu_id, min_level, max_level,"
                " faction, npc_flags, unit_flags, unit_class, unit_gender,"
                " scale, type, family, display_id1, base_health)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry, name, subname, menu, level, level, faction, npc_flags,
                 unit_flags, 1, gender, scale, ctype, family, display, health),
            )
        for (guid, entry, map_id, zone, x, y, z, o) in _CREATURE_SPAWNS:
            con.execute(
                "INSERT OR IGNORE INTO creature_spawns("
                "guid, entry, map, zone, x, y, z, o)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (guid, entry, map_id, zone, x, y, z, o),
            )
        for (menu_id, text_id) in _GOSSIP_MENUS:
            con.execute(
                "INSERT OR IGNORE INTO gossip_menus(menu_id, text_id) VALUES(?,?)",
                (menu_id, text_id),
            )
        for option in _GOSSIP_OPTIONS:
            con.execute(
                "INSERT OR IGNORE INTO gossip_options("
                "menu_id, option_index, option_type, icon, coded, box_money,"
                " text, box_text, action_menu_id, action_text_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                option,
            )
        for text in _NPC_TEXT:
            con.execute(
                "INSERT OR IGNORE INTO npc_text(id, text, female_text)"
                " VALUES(?,?,?)",
                (text[0], text[1], text[1]),
            )
        con.commit()
    finally:
        con.close()


_SPAWN_SELECT = """
SELECT s.guid, s.entry, s.map, s.zone, s.x, s.y, s.z, s.o,
       s.display_id AS spawn_display_id,
       s.current_health, s.current_power, s.combat_state,
       s.target_guid, s.dead_since, s.loot_json,
       s.movement_type AS spawn_movement_type, s.spawndist,
       t.name, t.subname, t.gossip_menu_id, t.min_level, t.max_level,
       t.faction, t.npc_flags, t.unit_flags, t.unit_flags2, t.unit_class,
       t.unit_gender, t.unit_power_type, t.scale, t.type, t.family,
       t.classification, t.display_id1, t.display_id2, t.display_id3,
       t.display_id4, t.hp_multiplier, t.energy_multiplier, t.leader,
       t.movement_info_id, t.bounding_radius, t.combat_reach, t.base_health,
       t.movement_type AS template_movement_type,
       t.respawn_time, t.aggro_radius, t.loot_id,
       t.spell_list_id, t.ai_name, t.chase_distance,
       t.speed_walk, t.speed_run, t.min_damage, t.max_damage,
       t.attack_power, t.ranged_attack_power, t.armor,
       t.melee_attack_speed, t.ranged_attack_speed, t.crit_chance
FROM creature_spawns s
JOIN creature_templates t ON t.entry = s.entry
"""


def get_creature_spawns(path: str, map_id: int) -> List[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            _SPAWN_SELECT + " WHERE s.map = ? AND t.visible <> 0 ORDER BY s.guid",
            (int(map_id),)
        ).fetchall()
    finally:
        con.close()


def get_creature_spawns_near(path: str, map_id: int, x: float, y: float,
                             z: float, radius: float) -> List[sqlite3.Row]:
    """Return static creatures within ``radius`` yards of ``(x, y, z)``.

    A full 3.3.5 world has ~100k spawn points; sending every spawn on the map
    at login would be both large and useless.  The bounding-box predicate uses
    ``idx_creature_spawns_map_xy`` and the exact 3D distance check is done in
    Python (SQLite has no built-in sqrt and the result set is small after the
    index lookup).

    ``creature_templates.visible`` is 0 for Blizzard's tooling creatures
    (invisible stalkers, bunnies, quest triggers, "[DND]" props).  They stay in
    the database but are never sent, which is what keeps the starting zones free
    of invisible units and Karazhan pedestal props.
    """
    radius = max(0.0, float(radius))
    if radius <= 0.0:
        return []
    x, y, z = float(x), float(y), float(z)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            _SPAWN_SELECT
            + " WHERE s.map = ? AND t.visible <> 0"
            " AND s.x BETWEEN ? AND ? AND s.y BETWEEN ? AND ?"
            " ORDER BY s.guid",
            (int(map_id), x - radius, x + radius, y - radius, y + radius),
        ).fetchall()
    finally:
        con.close()
    limit = radius * radius
    near = []
    for row in rows:
        dx = float(row["x"]) - x
        dy = float(row["y"]) - y
        dz = float(row["z"]) - z
        if dx * dx + dy * dy + dz * dz <= limit:
            near.append(row)
    return near


def get_creature_spawn(path: str, guid: int) -> Optional[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            _SPAWN_SELECT + " WHERE s.guid = ?", (int(guid),)
        ).fetchone()
    finally:
        con.close()


def get_creature_template(path: str, entry: int) -> Optional[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM creature_templates WHERE entry = ?", (int(entry),)
        ).fetchone()
    finally:
        con.close()


def get_gossip_menu(path: str, menu_id: int) -> Optional[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM gossip_menus WHERE menu_id = ?", (int(menu_id),)
        ).fetchone()
    finally:
        con.close()


def get_gossip_options(path: str, menu_id: int) -> List[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM gossip_options WHERE menu_id = ? ORDER BY option_index",
            (int(menu_id),),
        ).fetchall()
    finally:
        con.close()


def get_gossip_option(path: str, menu_id: int, option_index: int) -> Optional[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM gossip_options WHERE menu_id = ? AND option_index = ?",
            (int(menu_id), int(option_index)),
        ).fetchone()
    finally:
        con.close()


def get_creature_movement(path: str, guid: int) -> List[sqlite3.Row]:
    """Return the per-spawn waypoint path for ``guid`` (``creature_movement``)."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT point, position_x, position_y, position_z, orientation,"
            " wait_time FROM creature_movement WHERE id = ? ORDER BY point",
            (int(guid),),
        ).fetchall()
    finally:
        con.close()


def get_creature_movement_template(path: str, entry: int,
                                   path_id: int = 0) -> List[sqlite3.Row]:
    """Return a shared waypoint path (``creature_movement_template``)."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT point, position_x, position_y, position_z, orientation,"
            " wait_time FROM creature_movement_template"
            " WHERE entry = ? AND path_id = ? ORDER BY point",
            (int(entry), int(path_id)),
        ).fetchall()
        if not rows and int(path_id) != 0:
            # Some dumps store the only path for an entry under path id 0.
            rows = con.execute(
                "SELECT point, position_x, position_y, position_z, orientation,"
                " wait_time FROM creature_movement_template"
                " WHERE entry = ? AND path_id = 0 ORDER BY point",
                (int(entry),),
            ).fetchall()
        return rows
    finally:
        con.close()


def get_npc_text(path: str, text_id: int) -> Optional[sqlite3.Row]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM npc_text WHERE id = ?", (int(text_id),)
        ).fetchone()
    finally:
        con.close()

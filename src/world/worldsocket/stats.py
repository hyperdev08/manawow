"""Derived stats, health/power, regeneration, XP and combo points."""
from __future__ import annotations

import math
import sqlite3
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import databases
from src.world import login_packets
from src.world import object_update
from src.world import spells
from src.world import update_fields as F

from .constants import (
    attacker_rage_gain_tenths,
    calculate_rage_conversion,
    victim_rage_gain_tenths,
)

class StatsMixin:
    # Rough per-class health pool.  A real core computes this from the character
    # stats; the important part here is that health scales with level so the
    # creature damage numbers and the XP curve stay in proportion.
    _HEALTH_PER_CLASS = {1: 22, 2: 20, 3: 14, 4: 16, 5: 15, 6: 20, 7: 15,
                         8: 13, 9: 14, 11: 15}

    # ``UnitField::POWER1`` is *not* in displayed resource points for every
    # power type.  Rage and runic power are stored in tenths (a full bar is
    # 1000, ``Unit::GetCreatePowerValue``), energy is 0..100 and mana is plain
    # points.  Keeping rage in the same 0..100 range as mana makes a 15-rage
    # ability look like it costs 150 of a 100-point bar, so the client greys
    # the button out and never even sends the cast.
    _RAGE_CLASSES = (1, 6)          # warrior (rage), death knight (runic power)

    _ENERGY_CLASSES = (4,)          # rogue

    # Rough per-class mana pool; only needs to keep spell costs reachable as the
    # character levels, in the same spirit as ``_HEALTH_PER_CLASS``.
    _MANA_BASE = 60

    _MANA_PER_CLASS = {2: 8, 3: 9, 5: 11, 7: 11, 8: 12, 9: 11, 11: 9}

    _MANA_PER_LEVEL = 10

    # Tauren ``Endurance`` (spell 20550) is a passive racial aura that raises
    # total health by 5%.  It is the only racial health modifier in 3.3.5a.
    _RACE_HEALTH_PCT = {6: 1.05}

    # Mana regeneration in 3.3.5 is spirit driven: the real core publishes
    # ``sqrt(Intellect) * Spirit * BASE_REGEN[level]`` mana per second in
    # ``UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER`` (CMaNGOS ``UpdateManaRegen`` /
    # ``OCTRegenMPPerSpirit``) and adds the same amount every tick.  Casting a
    # spell that spends mana drops the character into the "interrupted" rate for
    # five seconds; without talents that rate is zero, so a Fireball's cost
    # actually sticks to the bar instead of being refilled instantly.
    _MANA_REGEN_INTERRUPT_SECONDS = 5.0

    # ``Player::Regenerate(POWER_RAGE)``: rage only decays, only out of combat,
    # at ``(diff_ms / 200) * 2.5`` rage per call, i.e. 25 rage (= 250 tenths)
    # every two seconds.  A warrior who stops attacking therefore loses the whole
    # bar in ~8 s, which is the real 3.3.5 behaviour and is what makes a
    # permanent full Rage meter impossible.
    _RAGE_DECAY_INTERVAL = 2.0

    _RAGE_DECAY_TENTHS = 250

    # ``Player::Regenerate(POWER_RUNIC_POWER)`` decays 2.5 runic power per two
    # seconds, i.e. 25 tenths, which is ten times slower than rage.
    _RUNIC_POWER_DECAY_TENTHS = 25

    # ``Player::Regenerate(POWER_ENERGY)``: ``(diff_ms / 100) * rate``, i.e.
    # 10 energy per second.
    _ENERGY_PER_SECOND = 10.0

    def _max_health(self) -> int:
        return max(1, int(self.character.get("max_health", 100))) if self.character else 100

    def _max_power(self) -> int:
        return max(1, int(self.character.get("max_power", 100))) if self.character else 100

    def _create_mana(self) -> int:
        """``Unit::GetCreateMana()`` - the class/level pool without the intellect bonus.

        ``UNIT_FIELD_BASE_MANA`` is what the 3.3.5 client uses for the same
        number, and ``Spell::CalculatePowerCost`` multiplies a spell's
        ``ManaCostPercentage`` by *that*, not by ``GetMaxPower``.  Charging the
        percentage against the finished pool (which carries the intellect bonus)
        would make every cast cost more than the client just predicted, so its
        bar would keep sliding further down than the tooltip promised.
        """
        if not self.character:
            return 100
        base = int(self.character.get("base_mana", 0) or 0)
        return base if base > 0 else max(1, int(self.character.get("max_power", 100)))

    def _max_power_for(self, char_class: int, level: int) -> int:
        """The ``UNIT_FIELD_MAXPOWER1`` value for a class at a given level.

        For mana classes, uses the real CMaNGOS ``player_classlevelstats.basemana``
        (the ``UNIT_FIELD_BASE_MANA`` value) instead of a flat formula.  Rage/runic
        classes use 1000 (tenths), energy classes use 100.
        """
        if char_class in self._RAGE_CLASSES:
            return 1000                       # 100 rage / runic power, in tenths
        if char_class in self._ENERGY_CLASSES:
            return 100
        # Look up the real base mana from the imported table (level 1 is the base).
        # If not found, fall back to a reasonable default.
        base = self._class_basemana(char_class, level)
        if base:
            return base
        return self._MANA_BASE + (level - 1) * self._MANA_PER_CLASS.get(
            char_class, self._MANA_PER_LEVEL)

    def _rage_conversion(self) -> float:
        """``Player::CalculateRageConversion`` - damage that yields one rage."""
        level = max(1, int(self.character.get("level", 1))) if self.character else 1
        return calculate_rage_conversion(level)

    def _power_after_reset(self) -> int:
        """Power to grant when the player is refilled (login, level up, respawn).

        Rage and runic power are combat resources and always come back empty -
        TrinityCore resets them to zero on death, resurrection and level up -
        while mana and energy pools return full.
        """
        if not self.character:
            return 0
        if int(self.character.get("class", 1)) in self._RAGE_CLASSES:
            return 0
        return int(self.character.get("max_power", 100))

    def _load_player_level_stats(self, race: int, char_class: int,
                                 level: int) -> tuple[dict | None, int, int]:
        """Read base attributes, base mana and base health from CMaNGOS tables.

        Returns ``(stats, base_mana, base_health)`` where ``stats`` is the five
        primary attributes (``str``/``agi``/``sta``/``inte``/``spi``),
        ``base_mana`` is ``player_classlevelstats.basemana`` and
        ``base_health`` is ``player_classlevelstats.basehp``.  Both class/level
        values are the core's ``GetCreateMana``/``GetCreateHealth``; stamina and
        intellect bonuses are added on top by :meth:`_apply_combat_stats`.

        The exact row is looked up by ``race/class/level``; if that combination
        is not in the dump (an illegal race/class pair such as a night-elf mage,
        which the real client forbids but our test tooling created) the nearest
        *races* for that class are tried, in order of race id distance, so every
        character still gets a real stat block instead of zeros.
        """
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT str, agi, sta, inte, spi FROM player_levelstats "
                "WHERE race=? AND class=? AND level=?",
                (race, char_class, level),
            ).fetchone()
            if row is None:
                # Fall back to the closest race for this class/level.
                candidates = con.execute(
                    "SELECT race, str, agi, sta, inte, spi FROM player_levelstats "
                    "WHERE class=? AND level=? ORDER BY ABS(race - ?)",
                    (char_class, level, race),
                ).fetchall()
                row = candidates[0][1:] if candidates else None
            class_row = con.execute(
                "SELECT basemana, basehp FROM player_classlevelstats "
                "WHERE class=? AND level=?",
                (char_class, level),
            ).fetchone()
        finally:
            con.close()
        stats = None
        if row:
            stats = {
                "str": int(row[0]), "agi": int(row[1]), "sta": int(row[2]),
                "inte": int(row[3]), "spi": int(row[4]),
            }
        base_mana = int(class_row[0]) if class_row else 0
        base_health = int(class_row[1]) if class_row else 0
        return stats, base_mana, base_health

    @staticmethod
    def _health_bonus_from_stamina(stamina: float) -> float:
        """``Unit::GetHealthBonusFromStamina`` (WotLK 3.3.5a).

        The first 20 stamina are worth one health each; every point above 20 is
        worth ten.  Kept in floating point to match the C++ source exactly.
        """
        base_stam = stamina if stamina < 20 else 20.0
        more_stam = stamina - base_stam
        return base_stam + (more_stam * 10.0)

    @staticmethod
    def _mana_bonus_from_intellect(intellect: float) -> float:
        """``Unit::GetManaBonusFromIntellect`` (WotLK 3.3.5a).

        The first 20 intellect are worth one mana each; every point above 20 is
        worth fifteen.
        """
        base_int = intellect if intellect < 20 else 20.0
        more_int = intellect - base_int
        return base_int + (more_int * 15.0)

    @staticmethod
    def _base_attack_power(char_class: int, level: int, stats: dict) -> float:
        """``Player::UpdateAttackPowerAndDamage(false)`` class formula.

        Returns the value CMaNGOS writes to ``UNIT_FIELD_ATTACK_POWER`` (before
        flat item/aura modifiers).  Druids are handled without a shapeshift
        form, which is the only state this server supports.
        """
        level_f = float(level)
        strength = float(stats.get("str", 0) or 0)
        agility = float(stats.get("agi", 0) or 0)
        if char_class in (1, 2, 6, 11):        # warrior, paladin, DK, druid
            return level_f * 3.0 + strength * 2.0 - 20.0
        if char_class in (3, 4, 7):            # hunter, rogue, shaman
            return level_f * 2.0 + strength + agility - 20.0
        if char_class in (5, 8, 9):            # priest, mage, warlock
            return strength - 10.0
        return 0.0

    @staticmethod
    def _base_ranged_attack_power(char_class: int, level: int,
                                  stats: dict) -> float:
        """``Player::UpdateAttackPowerAndDamage(true)`` class formula."""
        level_f = float(level)
        agility = float(stats.get("agi", 0) or 0)
        if char_class == 3:                    # hunter
            return level_f * 2.0 + agility - 10.0
        if char_class in (1, 4):               # warrior, rogue
            return level_f + agility - 10.0
        return agility - 10.0

    @staticmethod
    def _armor_reduced_damage(damage: int, armor: int,
                              attacker_level: int) -> int:
        """``Unit::CalcArmorReducedDamage`` for normal-school melee.

        ``levelModifier`` grows after level 59, ``tmp`` is the fraction of the
        hit absorbed by armor (capped at 75%), and a hit never drops below 1.
        """
        armor = max(0.0, float(armor))
        level_modifier = float(max(1, attacker_level))
        if level_modifier > 59:
            level_modifier += 4.5 * (level_modifier - 59)
        tmp = 0.1 * armor / (8.5 * level_modifier + 40.0)
        tmp = tmp / (1.0 + tmp)
        tmp = min(0.75, max(0.0, tmp))
        new_damage = float(damage) - (float(damage) * tmp)
        return max(1, int(new_damage))

    def _apply_combat_stats(self) -> None:
        """Fill in the combat numbers the create block and the loop need.

        ``characters`` only stores level/xp/money, so health, power and the XP
        threshold are derived on login and kept in the in-memory character dict.
        Primary attributes come from the imported CMaNGOS ``player_levelstats``
        table (with a nearest-race fallback for race/class pairs the real client
        forbids) plus equipped-item bonuses; the health and mana pools mirror
        ``Unit::UpdateMaxHealth`` / ``Unit::UpdateMaxPower`` exactly::

            max_health = round((basehp + item_health
                                + GetHealthBonusFromStamina(sta))
                               * racial_health_pct)
            max_mana   = basemana + item_mana
                         + GetManaBonusFromIntellect(inte)

        ``basehp``/``basemana`` are the class/level ``GetCreateHealth`` /
        ``GetCreateMana`` values, so ``UNIT_FIELD_BASE_HEALTH`` stays the raw
        ``basehp`` (20 for a level-1 warrior) rather than the finished pool.
        """
        if not self.character:
            return
        level = max(1, int(self.character.get("level", 1)))
        char_class = int(self.character.get("class", 1))
        race = int(self.character.get("race", 1))
        stats, base_mana, base_health = self._load_player_level_stats(
            race, char_class, level)
        if stats is None:
            stats = {"str": 0, "agi": 0, "sta": 0, "inte": 0, "spi": 0}

        # Equipped items contribute exactly like the item auras CMaNGOS applies
        # in ``Player::_ApplyItemMods``: primary attributes (which then feed the
        # stamina/intellect pool formulas below), flat health/mana, attack
        # power and armor/resistances.  Only equipment slots 0..18 count.
        try:
            item_totals = character_db.get_equipped_item_totals(
                self.config.database.world_path, int(self.character["guid"]))
        except (KeyError, TypeError, sqlite3.Error):
            item_totals = {}
        for key in ("str", "agi", "sta", "inte", "spi"):
            stats[key] = int(stats.get(key, 0) or 0) + int(
                item_totals.get(key, 0) or 0)
        # Aura stat modifiers (Arcane Intellect, Power Word: Fortitude, ...)
        # are part of the final attribute values, exactly like item auras.
        aura_mods = (self._player_aura_mods()
                     if hasattr(self, "_player_aura_mods") else None)
        if aura_mods:
            for key in ("str", "agi", "sta", "inte", "spi"):
                stats[key] = int(stats.get(key, 0) or 0) + int(
                    aura_mods["stats"].get(key, 0) or 0)
            # Percentage stat buffs (Blessing of Kings' total-stat percentage
            # and the single-stat percent aura) are applied after all flat
            # contributions, matching ``Unit::UpdateStats``.
            total_pct = float(aura_mods.get("total_stat_pct", 0.0) or 0.0)
            for key in ("str", "agi", "sta", "inte", "spi"):
                pct = float(aura_mods["stats_pct"].get(key, 0.0) or 0.0)
                if total_pct or pct:
                    stats[key] = max(0, int(round(
                        float(stats.get(key, 0) or 0)
                        * (1.0 + (total_pct + pct) / 100.0))))
        self.character["stats"] = stats

        item_health = int(item_totals.get("health", 0) or 0)
        item_mana = int(item_totals.get("mana", 0) or 0)

        old_max_health = int(self.character.get("max_health", 0) or 0)
        old_health = int(self.character.get("health", 0) or 0)

        if base_health > 0:
            stamina = float(stats.get("sta", 0))
            # ``Unit::UpdateMaxHealth``: base + flat bonuses + stamina, then the
            # racial total-percent modifier (Tauren Endurance, 5%).
            health_value = (base_health + item_health
                            + self._health_bonus_from_stamina(stamina))
            if aura_mods:
                health_value += aura_mods["health"]
                health_value *= 1.0 + float(
                    aura_mods.get("health_pct", 0.0) or 0.0) / 100.0
            health_value *= self._RACE_HEALTH_PCT.get(race, 1.0)
            # ``std::round`` rounds halves away from zero; Python's round()
            # uses banker's rounding, so spell it out for exact parity.
            max_health = max(1, int(math.floor(health_value + 0.5)))
        else:
            # No imported row: keep the old level-scaled approximation so a
            # custom character still has a usable pool.
            per_level = self._HEALTH_PER_CLASS.get(char_class, 15)
            max_health = 100 + (level - 1) * per_level + item_health
        self.character["max_health"] = max_health
        # Keep the current bar: a full bar stays full, a damaged (or dead)
        # character keeps its absolute value, clamped to the new maximum.
        if "health" not in self.character \
                or (old_max_health > 0 and old_health >= old_max_health):
            health = max_health
        else:
            health = min(old_health, max_health)
        self.character["health"] = health

        # Rage/runic classes use the 0..1000 (tenths) scale, energy 0..100 and
        # mana grows with level.  See ``_max_power_for``.  ``Unit::UpdateMaxPower``
        # adds ``GetManaBonusFromIntellect`` and flat item mana to the class base
        # pool (20 intellect worth 1 mana each, everything above 20 worth 15),
        # which is why a level-1 mage has 165 mana rather than the raw 100.
        old_max_power = int(self.character.get("max_power", 0) or 0)
        old_power = int(self.character.get("power", 0) or 0)
        max_power = self._max_power_for(char_class, level)
        if char_class in self._ENERGY_CLASSES and aura_mods:
            max_power += int(aura_mods["power"] or 0)
            max_power = int(max_power * (
                1.0 + float(aura_mods.get("power_pct", 0.0) or 0.0) / 100.0))
        if char_class not in self._RAGE_CLASSES \
                and char_class not in self._ENERGY_CLASSES:
            intellect = max(0, int(stats.get("inte", 0) or 0))
            max_power += item_mana + int(math.floor(
                self._mana_bonus_from_intellect(intellect) + 0.5))
            if aura_mods:
                max_power += int(aura_mods["power"] or 0)
                max_power = int(max_power * (
                    1.0 + float(aura_mods.get("power_pct", 0.0) or 0.0)
                    / 100.0))
        self.character["max_power"] = max_power
        if "power" not in self.character:
            # Combat resources start empty; mana/energy classes start full.
            power = 0 if char_class in self._RAGE_CLASSES else max_power
        elif (old_max_power > 0 and old_power >= old_max_power
              and char_class not in self._RAGE_CLASSES):
            power = max_power
        else:
            power = min(old_power, max_power)
        self.character["power"] = max(0, power)

        # ``Player::UpdateArmor``: item armor plus twice the final agility.
        agility = int(stats.get("agi", 0) or 0)
        armor = int(item_totals.get("armor", 0) or 0) + agility * 2
        if aura_mods:
            armor += int(aura_mods["armor"] or 0)
            armor = int(armor * (
                1.0 + float(aura_mods.get("armor_pct", 0.0) or 0.0) / 100.0))
        self.character["armor"] = armor
        for school in ("holy", "fire", "nature", "frost", "shadow", "arcane"):
            value = int(item_totals.get(school, 0) or 0)
            if aura_mods:
                value += int(aura_mods["resistances"].get(school, 0) or 0)
                value = int(value * (
                    1.0 + float(
                        aura_mods.get("resistances_pct", {}).get(school, 0.0)
                        or 0.0) / 100.0))
            self.character["res_" + school] = value

        # ``Player::UpdateAttackPowerAndDamage``: class/level/stat formula plus
        # flat item attack power.  Ranged AP uses its own class formula and the
        # ``ITEM_MOD_RANGED_ATTACK_POWER`` bonus.
        attack_power = int(self._base_attack_power(char_class, level, stats))
        attack_power += int(item_totals.get("attack_power", 0) or 0)
        if aura_mods:
            attack_power += int(aura_mods["attack_power"] or 0)
            attack_power = int(attack_power * (
                1.0 + float(aura_mods.get("attack_power_pct", 0.0) or 0.0)
                / 100.0))
        self.character["attack_power"] = attack_power
        ranged_ap = int(self._base_ranged_attack_power(
            char_class, level, stats))
        ranged_ap += int(item_totals.get("ranged_attack_power", 0) or 0)
        if aura_mods:
            ranged_ap += int(aura_mods["ranged_attack_power"] or 0)
            ranged_ap = int(ranged_ap * (
                1.0 + float(
                    aura_mods.get("ranged_attack_power_pct", 0.0) or 0.0)
                / 100.0))
        self.character["ranged_attack_power"] = ranged_ap

        # Combat ratings and derived chances.  Ratings are converted with a
        # flat 15 points per percent (the level-60-ish ratio) because the full
        # ``GtCombatRatings`` curve is not imported; the result is still a
        # consistent, gear-driven table rather than fixed 5%/10%.
        hit_rating = int(item_totals.get("hit_rating", 0) or 0)
        crit_rating = int(item_totals.get("crit_rating", 0) or 0)
        haste_rating = int(item_totals.get("haste_rating", 0) or 0)
        self.character["hit_rating"] = hit_rating
        self.character["crit_rating"] = crit_rating
        self.character["haste_rating"] = haste_rating
        self.character["resilience"] = int(item_totals.get("resilience", 0) or 0)
        self.character["armor_penetration"] = int(
            item_totals.get("armor_penetration", 0) or 0)
        self.character["expertise"] = int(
            item_totals.get("expertise", 0) or 0)
        self.character["defense_rating"] = int(
            item_totals.get("defense_rating", 0) or 0)
        self.character["has_shield"] = bool(item_totals.get("shield", 0))
        self.character["block_value"] = int(
            item_totals.get("block_value", 0) or 0)
        self.character["spell_power"] = int(
            item_totals.get("spell_power", 0) or 0)
        self.character["healing_power"] = int(
            item_totals.get("healing_power", 0) or 0)
        if aura_mods:
            self.character["spell_power"] += int(
                aura_mods.get("spell_power", 0) or 0)
            self.character["healing_power"] += int(
                aura_mods.get("healing", 0) or 0)
            self.character["healing_pct"] = float(
                aura_mods.get("healing_pct", 0.0) or 0.0)
        else:
            self.character["healing_power"] = 0
            self.character["healing_pct"] = 0.0
        agility = int(stats.get("agi", 0) or 0)
        aura_hit = float(aura_mods.get("hit", 0.0)) if aura_mods else 0.0
        aura_spell_hit = float(aura_mods.get("spell_hit", 0.0)) \
            if aura_mods else 0.0
        aura_crit = float(aura_mods.get("crit", 0.0)) if aura_mods else 0.0
        aura_spell_crit = float(aura_mods.get("spell_crit", 0.0)) \
            if aura_mods else 0.0
        self.character["hit_chance"] = min(
            100.0, 5.0 + hit_rating / 15.0 + aura_hit)
        self.character["spell_hit_chance"] = min(
            100.0, 5.0 + hit_rating / 15.0 + aura_spell_hit)
        self.character["crit_chance"] = min(
            75.0, 5.0 + crit_rating / 15.0 + agility * 0.005 + aura_crit)
        self.character["spell_crit_chance"] = min(
            75.0, 5.0 + crit_rating / 15.0 + aura_spell_crit)
        self.character["dodge_chance"] = min(
            75.0, 5.0 + int(item_totals.get("dodge_rating", 0) or 0) / 15.0
            + agility * 0.002 + (float(aura_mods.get("dodge", 0.0))
                                 if aura_mods else 0.0))
        self.character["parry_chance"] = min(
            75.0, 5.0 + int(item_totals.get("parry_rating", 0) or 0) / 15.0
            + (float(aura_mods.get("parry", 0.0)) if aura_mods else 0.0))
        self.character["block_chance"] = (
            min(75.0, 5.0 + int(item_totals.get("block_rating", 0) or 0) / 15.0
                + (float(aura_mods.get("block", 0.0)) if aura_mods else 0.0))
            if self.character["has_shield"] else 0.0)
        self.character["melee_haste"] = haste_rating / 15.0 + (
            float(aura_mods.get("melee_haste", 0.0)) if aura_mods else 0.0)
        self.character["ranged_haste"] = haste_rating / 15.0 + (
            float(aura_mods.get("ranged_haste", 0.0)) if aura_mods else 0.0)
        self.character["spell_haste"] = haste_rating / 15.0 + (
            float(aura_mods.get("spell_haste", 0.0)) if aura_mods else 0.0)
        self.character["spell_penetration"] = int(
            item_totals.get("spell_penetration", 0) or 0) + int(
            aura_mods.get("spell_penetration", 0) if aura_mods else 0)
        self.character["health_regen_pct"] = float(
            aura_mods.get("health_regen_pct", 0.0) if aura_mods else 0.0)

        self.character["next_level_xp"] = self._xp_to_level(level)
        self.character.setdefault("money", 0)
        # ``UNIT_FIELD_BASE_MANA``/``BASE_HEALTH`` are the unmodified
        # class/level pools (0 for rage and energy users' mana).  The stamina
        # and intellect bonuses live only in the finished max values.
        self.character["base_mana"] = (
            0 if (char_class in self._RAGE_CLASSES
                  or char_class in self._ENERGY_CLASSES) else base_mana
        )
        self.character["base_health"] = base_health or max_health

    def _class_basemana(self, char_class: int, level: int) -> int:
        """``player_classlevelstats.basemana`` for a class/level (0 for rage)."""
        if char_class in self._RAGE_CLASSES:
            return 0
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT basemana FROM player_classlevelstats WHERE class=? AND level=?",
                (char_class, level),
            ).fetchone()
        finally:
            con.close()
        return int(row[0]) if row else 0

    async def _send_player_fields(self, fields: dict) -> None:
        """Send one ``UPDATETYPE_VALUES`` update for the player.

        ``Unit::SetPower`` always sends ``SMSG_POWER_UPDATE`` alongside the
        update object block, and the 3.3.5 mana/energy/rage bar UI refreshes
        from that packet (it fires the ``UNIT_POWER`` event the bar widget
        listens to).  The mirrored update field alone is not enough: without
        the power-update packet the bar drops on a spell cost and never climbs
        back during regeneration, which is exactly the "mana doesn't
        regenerate" symptom.  The original stutter came from the cost *basis*
        being wrong (charging against the finished pool instead of the base),
        not from sending this packet on every tick.
        """
        if not self.character:
            return
        opcode, payload = object_update.build_player_values_update(
            self.character, fields, force_uncompressed=True
        )
        await self.send_packet(opcode, payload)
        # ``Unit::SetPower`` sends SMSG_POWER_UPDATE in addition to the update
        # object block; the client's rage/energy/mana bar reacts to this packet,
        # not (only) to the mirrored field.  Power type is 0 mana, 1 rage,
        # 3 energy, 6 runic power.
        power_field = self._power_field(F.UNIT_FIELD_POWER1)
        if power_field in fields:
            await self.send_packet(
                Opcodes.SMSG_POWER_UPDATE,
                spells.pack_guid(int(self.player_guid))
                + struct.pack("<BI", self._power_type(),
                              int(fields[power_field]) & 0xFFFFFFFF),
            )

    def _power_type(self) -> int:
        """Power type for the player's class (``Powers`` enum)."""
        char_class = int(self.character.get("class", 1)) if self.character else 1
        if char_class in self._RAGE_CLASSES:
            return 6 if char_class == 6 else 1     # runic power / rage
        if char_class in self._ENERGY_CLASSES:
            return 3                                # energy
        return 0                                    # mana

    def _power_field(self, base: int) -> int:
        """Update-field index for this class's power slot.

        ``UNIT_FIELD_POWER1``/``MAXPOWER1`` are arrays indexed by power type:
        mana is +0, rage +1, focus +2, energy +3, happiness +4, runes +5 and
        runic power +6.  The client reads only its own class's slot, so a
        warrior's rage must be written to ``UNIT_FIELD_POWER2``.
        """
        return base + self._power_type()

    def _derived_player_fields(self) -> dict:
        """Stat, base-pool and mana-regen fields that follow level/stat changes.

        These are normally carried by the create block; resending them after a
        level-up keeps the character sheet and the client's mana-regen
        prediction in sync without a relog.
        """
        fields = {}
        if not self.character:
            return fields
        stats = self.character.get("stats") or {}
        for index, key in enumerate(("str", "agi", "sta", "inte", "spi")):
            fields[F.UNIT_FIELD_STAT0 + index] = int(stats.get(key, 0) or 0)
        fields[F.UNIT_FIELD_BASE_MANA] = int(self.character.get("base_mana", 0) or 0)
        fields[F.UNIT_FIELD_BASE_HEALTH] = int(
            self.character.get("base_health", 0) or 0
        )
        if self._power_type() == 0 and stats:
            level = max(1, int(self.character.get("level", 1)))
            flat_bits, interrupted_bits = object_update.mana_regen_rates(
                stats, level
            )
            fields[F.UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER] = flat_bits
            fields[F.UNIT_FIELD_POWER_REGEN_INTERRUPTED_FLAT_MODIFIER] = \
                interrupted_bits
        return fields

    async def _send_level_up_packet(self, health_delta: int = 0) -> None:
        """Send the 56-byte ``SMSG_LEVELUP_INFO``."""
        level = int(self.character.get("level", 1))
        power_delta = [0] * 7
        stat_delta = [0] * 5
        payload = struct.pack("<II", level, max(0, int(health_delta)))
        payload += struct.pack("<7I", *power_delta)
        payload += struct.pack("<5I", *stat_delta)
        await self.send_packet(Opcodes.SMSG_LEVELUP_INFO, payload)

    def _queue_player_fields(self, fields: dict) -> None:
        """Remember player fields that a synchronous helper had to change.

        Rage gain happens inside helpers that cannot await (they are called from
        the swing/damage paths), so the value is queued here and flushed once per
        combat tick and once per handled packet.
        """
        pending = getattr(self, "_pending_player_fields", None)
        if pending is None:
            pending = self._pending_player_fields = {}
        pending.update(fields)

    async def _flush_player_fields(self) -> None:
        """Send everything queued by :meth:`_queue_player_fields`."""
        pending = getattr(self, "_pending_player_fields", None)
        if not pending:
            return
        self._pending_player_fields = {}
        await self._send_player_fields(pending)

    async def _set_player_health(self, health: int) -> None:
        """Store the player's health and push it to the client.

        No packet is sent when the value did not change: the regen tick runs
        four times a second and an empty ``UPDATETYPE_VALUES`` update per tick
        would flood the client's object updater.
        """
        if not self.character:
            return
        health = max(0, min(self._max_health(), int(health)))
        if health == int(self.character.get("health", 0)):
            return
        self.character["health"] = health
        await self._send_player_fields({F.UNIT_FIELD_HEALTH: health})

    async def _set_player_power(self, power: int) -> None:
        """Store the player's power (mana/rage) and push it to the client."""
        if not self.character:
            return
        power = max(0, min(self._max_power(), int(power)))
        current = int(self.character.get("power", 0))
        if power == current:
            # Nothing changed: skip the update entirely.  Without this guard a
            # warrior whose rage is already capped would get one redundant
            # SMSG_POWER_UPDATE per swing, which floods the client's power-bar
            # handler and makes the bar look frozen at full.
            return
        previous = self.character.get("power")
        self.character["power"] = power
        # The regen tick would flood the log at four lines a second, so only a
        # power *decrease* (a spell cost or rage decay) is worth an INFO line.
        log = self.log.info if power < int(previous or 0) else self.log.debug
        log(
            f"power {previous} -> {power} of {self._max_power()} "
            f"(type {self._power_type()})"
        )
        await self._send_player_fields(
            {self._power_field(F.UNIT_FIELD_POWER1): power},
        )

    def _xp_to_level(self, level: int) -> int:
        """Approximate the 3.3.5 level curve used by the client XP bar."""
        return 400 + max(0, int(level) - 1) * 500

    async def _award_xp(self, xp: int, victim_guid: int = 0) -> None:
        """Award XP to the player and handle level-up."""
        if not self.character:
            return
        level = int(self.character.get("level", 1))
        total_xp = int(self.character.get("xp", 0)) + int(xp)
        await self.send_packet(
            Opcodes.SMSG_LOG_XPGAIN,
            struct.pack("<QIBIfB", int(victim_guid), total_xp, 0, int(xp), 1.0, 0),
        )
        threshold = self._xp_to_level(level)
        leveled = False
        while total_xp >= threshold and level < 80:
            total_xp -= threshold
            level += 1
            threshold = self._xp_to_level(level)
            leveled = True
            self.character["level"] = level
        self.character["xp"] = total_xp
        self.character["next_level_xp"] = threshold
        if leveled:
            # Recompute the derived pool and refill it, like the real core does
            # on level up; the client needs the deltas for its level-up text.
            old_max = int(self.character.get("max_health", 100))
            self._apply_combat_stats()
            new_max = int(self.character.get("max_health", old_max))
            self.character["health"] = new_max
            self.character["power"] = self._power_after_reset()
            await self._send_level_up_packet(new_max - old_max)
        con = databases.connect_character(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE characters SET level = ?, xp = ? WHERE guid = ?",
                (level, total_xp, self.player_guid),
            )
            con.commit()
        finally:
            con.close()
        fields = {
            F.PLAYER_XP: total_xp,
            F.PLAYER_NEXT_LEVEL_XP: threshold,
            F.UNIT_FIELD_LEVEL: level,
            F.UNIT_FIELD_MAXHEALTH: int(self.character.get("max_health", 100)),
            F.UNIT_FIELD_HEALTH: int(self.character.get("health", 100)),
            self._power_field(F.UNIT_FIELD_POWER1): int(self.character.get("power", 100)),
            self._power_field(F.UNIT_FIELD_MAXPOWER1): int(self.character.get("max_power", 100)),
        }
        if leveled:
            fields.update(self._derived_player_fields())
        await self._send_player_fields(fields)
        if leveled and hasattr(self, "refresh_gameobject_quest_flags"):
            # New quests may unlock: wanted posters become clickable.
            await self.refresh_gameobject_quest_flags()
        if leveled:
            self.log.info(
                f"'{self.character['name']}' reached level {level} "
                f"(health {self.character.get('health')}/"
                f"{self.character.get('max_health')}, power "
                f"{self.character.get('power')}/{self.character.get('max_power')}, "
                f"base mana {self.character.get('base_mana')}, regen "
                f"{self._mana_regen_rates()[0]:.2f}/s)"
            )

    def _combo_target(self) -> int:
        return int(self._get_combat_state().get("combo_target") or 0)

    def _combo_points(self) -> int:
        return int(self._get_combat_state().get("combo_points") or 0)

    async def _send_combo_points(self, target_guid: int, points: int) -> None:
        """``Player::SendComboPoints``: packed target guid + ``uint8`` points.

        The client keys the combo-point display off the target guid, so the
        packet must name the unit the points belong to, not the player.
        """
        if not target_guid:
            return
        await self.send_packet(
            Opcodes.SMSG_UPDATE_COMBO_POINTS,
            spells.pack_guid(int(target_guid)) + struct.pack("<B", int(points) & 0xFF),
        )

    async def _add_combo_points(self, target_guid: int, count: int) -> None:
        """``Unit::AddComboPoints``: add, switch target, cap at five."""
        if not self.character or int(count) <= 0:
            return
        target_guid = int(target_guid or 0)
        if not target_guid:
            return
        combat = self._get_combat_state()
        if target_guid == int(combat.get("combo_target") or 0):
            combat["combo_points"] = int(combat.get("combo_points") or 0) + int(count)
        else:
            # Opening on a new target drops the old target's points.
            combat["combo_target"] = target_guid
            combat["combo_points"] = int(count)
        combat["combo_points"] = max(0, min(5, int(combat["combo_points"])))
        await self._send_combo_points(target_guid, combat["combo_points"])
        self.log.debug(
            f"combo points on {target_guid}: {combat['combo_points']}"
        )
        if not self._unit_is_alive(target_guid):
            # The blow was fatal.  The core adds the points during the effect
            # loop (before ``DealDamage``), then the death's
            # ``ClearComboPointHolders`` wipes them; our damage effect runs
            # first, so replay the clear to end in the same state.
            await self._clear_combo_points()

    async def _clear_combo_points(self) -> None:
        """``Unit::ClearComboPoints``: send zero to the old target, then forget it.

        The core sends the packet *before* clearing ``m_comboTargetGuid`` (so
        the client still knows which unit's dots to empty) and does nothing at
        all when there is no combo target.
        """
        combat = self._get_combat_state()
        target_guid = int(combat.get("combo_target") or 0)
        if not target_guid:
            return
        combat["combo_points"] = 0
        await self._send_combo_points(target_guid, 0)
        combat["combo_target"] = 0
        self.log.debug(f"combo points cleared (target {target_guid})")

    async def _heal_player(self, amount: int,
                           healer_guid: int | None = None) -> tuple[int, int]:
        """Apply a heal; returns ``(effective, overheal)``."""
        current = int(self.character.get("health", self._max_health()))
        max_health = self._max_health()
        if current >= max_health:
            return 0, max(0, int(amount))
        effective = min(max_health - current, max(0, int(amount)))
        await self._set_player_health(current + effective)
        self._add_healing_threat(
            effective, int(self.player_guid or 0),
            int(healer_guid or self.player_guid or 0))
        return effective, int(amount) - effective

    def _add_healing_threat(self, effective: int, target_guid: int,
                            healer_guid: int) -> None:
        """Credit half of effective healing to every mob fighting the target."""
        manager = (self._creature_combat_manager()
                   if hasattr(self, "_creature_combat_manager") else None)
        if manager is None or effective <= 0:
            return
        manager.healing_threat(
            int(healer_guid or 0), int(target_guid or 0), float(effective))

    async def _damage_player(self, damage: int, source: str,
                             physical: bool = False) -> None:
        """Apply damage to the player and handle death.

        ``physical`` marks normal-school melee damage.  ``Unit::DealDamage``
        rewards victim rage only for that school, so spell and DoT damage must
        not fill the warrior's bar (they pass the default ``False``).
        """
        if not self.character or self._get_combat_state()["dead"]:
            return
        current = int(self.character.get("health", self._max_health()))
        amount = max(1, int(damage))
        new_health = max(0, current - amount)
        self.character["health"] = new_health
        await self._send_player_fields({F.UNIT_FIELD_HEALTH: new_health})
        combat = self._get_combat_state()
        # Casting pushback: damage delays a normal cast (up to two hits) and
        # shortens an active channel.  This is the 3.3.5 ``Spell::Delayed``
        # behaviour without the per-spell interrupt flags table.
        pending = combat.get("pending_cast")
        if pending and not combat["dead"]:
            pushbacks = int(pending.get("pushbacks") or 0)
            if pushbacks < 2:
                pending["pushbacks"] = pushbacks + 1
                cast_time = max(0.0, float(pending["finish_at"])
                                - time.monotonic())
                pending["finish_at"] = float(pending["finish_at"]) + min(
                    0.5, cast_time * 0.25)
        channel = combat.get("channel")
        if channel and not combat["dead"]:
            channel["expires"] = max(
                time.monotonic(),
                float(channel.get("expires", 0.0)) - 0.5)
        if physical:
            self._gain_rage(amount, dealing=False)
        if new_health <= 0:
            await self._on_player_killed(source)

    async def _on_player_killed(self, source: str) -> None:
        """Die: stop everything, tell the client, and offer a release point."""
        combat = self._get_combat_state()
        combat["dead"] = True
        combat["pending_cast"] = None
        combat["channel"] = None
        combat["school_lockout"] = {}
        combat["threat_redirect"] = None
        combat["dots"] = []
        combat.setdefault("area_effects", []).clear()
        if hasattr(self, "_clear_player_auras"):
            await self._clear_player_auras()
        else:
            combat["auras"] = []
        combat["creature_auras"] = {}
        # ``CombatStop`` clears the dying rogue's combo points.
        await self._clear_combo_points()
        if combat["target_guid"]:
            payload = (spells.pack_guid(int(self.player_guid))
                       + spells.pack_guid(int(combat["target_guid"]))
                       + struct.pack("<I", 1))
            await self.send_packet(Opcodes.SMSG_ATTACKSTOP, payload)
        combat["auto_attacking"] = False
        combat["target_guid"] = 0
        for low_guid in list(combat["aggro"]):
            await self._reset_creature(low_guid)

        # UNIT_FIELD_HEALTH is already 0; PLAYER_FLAGS/GHOST is not needed for
        # the client to show the death screen, it only needs the location.
        await self.send_packet(
            Opcodes.SMSG_DEATH_RELEASE_LOC,
            struct.pack(
                "<Ifff",
                int(self.character["map"]),
                float(self.character["x"]),
                float(self.character["y"]),
                float(self.character["z"]),
            ),
        )
        await self._send_system_message("You have died.")
        self.log.info(f"'{self.character['name']}' was killed by {source}")

    async def _send_system_message(self, text: str) -> None:
        """Print a yellow system line in the player's chat frame."""
        await self.send_packet(
            Opcodes.SMSG_MESSAGECHAT,
            login_packets.build_message_chat_payload(
                login_packets.CHAT_MSG_SYSTEM, 0, 0, "", 0, "", text,
            ),
        )

    @classmethod
    def _mana_regen_interrupted(cls, now: float, last_power_use: float) -> bool:
        """``Player::IsUnderLastManaUseEffect`` for the five-second rule.

        ``last_power_use == 0`` means the character has not cast a mana spell
        this session, so regeneration is never suppressed.
        """
        if not last_power_use:
            return False
        return (now - float(last_power_use)) < cls._MANA_REGEN_INTERRUPT_SECONDS

    async def _regenerate(self, now: float) -> None:
        """Refill health and power the way the 3.3.5 core does.

        Health only comes back out of combat.  Rage only decays.  Energy and
        mana refill in combat too, mana at the spirit-based rate and at the
        reduced "recently cast" rate for five seconds after any spell spends
        mana.  Fractional power is carried between ticks, otherwise a low-level
        character's sub-1-mana-per-tick regen would round down to nothing.
        """
        combat = self._get_combat_state()
        if combat["dead"]:
            return
        max_health = self._max_health()
        max_power = self._max_power()
        health = int(self.character.get("health", max_health))
        power = int(self.character.get("power", max_power))
        in_fight = bool(combat["aggro"]) or combat["auto_attacking"]
        char_class = int(self.character.get("class", 1))

        if not in_fight and health < max_health:
            # ``Player::Regenerate(POWER_HEALTH)``: out-of-combat health comes
            # from spirit with a small level component and is carried as a
            # fraction between ticks, so low-level characters still heal.
            stats = self.character.get("stats") or {}
            spirit = max(0.0, float(stats.get("spi", 0) or 0))
            rate = spirit * 0.025 + max(1, int(self.character.get("level", 1))) * 0.05
            if hasattr(self, "_player_aura_mods"):
                rate *= 1.0 + float(
                    self._player_aura_mods().get("health_regen_pct", 0.0)
                    or 0.0) / 100.0
            await self._regenerate_health_over_time(
                combat, health, max_health, rate, now)

        if char_class == 6:
            # ``CLASS_DEATH_KNIGHT``: runes regenerate all the time, runic power
            # only decays and only while out of combat.
            await self._regenerate_runes(now)
            if in_fight or power <= 0:
                combat["last_rage_decay"] = now
                return
            last = float(combat.get("last_rage_decay") or 0.0)
            if last <= 0.0:
                combat["last_rage_decay"] = now
                return
            elapsed = now - last
            if elapsed < self._RAGE_DECAY_INTERVAL:
                return
            ticks = int(elapsed // self._RAGE_DECAY_INTERVAL)
            combat["last_rage_decay"] = last + ticks * self._RAGE_DECAY_INTERVAL
            await self._set_player_power(
                max(0, power - ticks * self._RUNIC_POWER_DECAY_TENTHS)
            )
        elif char_class in self._RAGE_CLASSES:
            # Rage never regenerates: out of combat it simply decays, so a
            # warrior who stops fighting loses the bar instead of keeping a
            # permanent full Rage meter.  CMaNGOS only calls
            # ``Regenerate(POWER_RAGE)`` while ``!IsInCombat()`` and its
            # ``m_regenTimer`` keeps ticking during the fight, so restart the
            # two-second decay window here instead of counting combat time.
            if in_fight or power <= 0:
                combat["last_rage_decay"] = now
                return
            last = float(combat.get("last_rage_decay") or 0.0)
            if last <= 0.0:
                combat["last_rage_decay"] = now
                return
            elapsed = now - last
            if elapsed < self._RAGE_DECAY_INTERVAL:
                return
            ticks = int(elapsed // self._RAGE_DECAY_INTERVAL)
            combat["last_rage_decay"] = last + ticks * self._RAGE_DECAY_INTERVAL
            await self._set_player_power(
                max(0, power - ticks * self._RAGE_DECAY_TENTHS)
            )
        elif char_class in self._ENERGY_CLASSES:
            await self._regenerate_over_time(
                combat, "energy", power, max_power,
                self._ENERGY_PER_SECOND, now,
            )
        else:
            # CMaNGOS ``Player::Regenerate(POWER_MANA)``: the flat rate the
            # client sees in ``UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER`` is added
            # every second, except for five seconds after a mana spend when the
            # *interrupted* rate (``..._INTERRUPTED_FLAT_MODIFIER``) applies.
            # Regenerating from those two fields instead of recomputing anything
            # here is what makes the bar the client animates locally and the
            # values the server pushes agree with each other.
            interrupted = self._mana_regen_interrupted(
                now, float(combat.get("last_power_use") or 0.0)
            )
            flat_rate, interrupted_rate = self._mana_regen_rates()
            rate = interrupted_rate if interrupted else flat_rate
            await self._regenerate_over_time(
                combat, "mana", power, max_power, rate, now,
            )

    def _mana_regen_rates(self) -> tuple[float, float]:
        """``(UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER, ..._INTERRUPTED_...)``.

        Both numbers come from :func:`object_update.mana_regen_rates`, i.e.
        from the very floats that are inside the create block and the level-up
        update, so whatever the client's own mana tick does is the same
        arithmetic the regen loop performs.
        """
        if not self.character:
            return 0.0, 0.0
        stats = self.character.get("stats") or {}
        level = max(1, int(self.character.get("level", 1)))
        flat_bits, interrupted_bits = object_update.mana_regen_rates(stats, level)
        return object_update.float_from_bits(flat_bits), \
            object_update.float_from_bits(interrupted_bits)

    async def _regenerate_over_time(self, combat: dict, key: str, power: int,
                                    max_power: int, rate: float,
                                    now: float) -> None:
        """Add ``rate`` power per second, carrying fractional points."""
        last_key = f"last_{key}_gain"
        accum_key = f"{key}_regen_accum"
        last = float(combat.get(last_key) or 0.0)
        if last <= 0.0:
            combat[last_key] = now
            return
        elapsed = max(0.0, now - last)
        combat[last_key] = now
        if rate <= 0.0 or power >= max_power:
            combat[accum_key] = 0.0
            return
        accum = float(combat.get(accum_key) or 0.0) + rate * elapsed
        whole = int(accum)
        combat[accum_key] = accum - whole
        if whole > 0:
            await self._set_player_power(min(max_power, power + whole))

    async def _regenerate_health_over_time(self, combat: dict, health: int,
                                           max_health: int, rate: float,
                                           now: float) -> None:
        """Add ``rate`` health per second, carrying fractional points."""
        last = float(combat.get("last_health_gain") or 0.0)
        if last <= 0.0:
            combat["last_health_gain"] = now
            return
        elapsed = max(0.0, now - last)
        combat["last_health_gain"] = now
        if rate <= 0.0 or health >= max_health:
            combat["health_regen_accum"] = 0.0
            return
        accum = float(combat.get("health_regen_accum") or 0.0) + rate * elapsed
        whole = int(accum)
        combat["health_regen_accum"] = accum - whole
        if whole > 0:
            await self._set_player_health(min(max_health, health + whole))

    def _gain_rage(self, damage: int, dealing: bool = True,
                   weapon_speed: float = 2.0, crit: bool = False) -> None:
        """Warriors generate rage when they deal or take melee damage.

        Mirrors CMaNGOS ``Unit::RewardRage``/``Player::RewardRage``: rage is
        ``damage / rageconversion * 7.5 + weaponSpeedFactor`` halved when the
        warrior is the attacker (``damage / rageconversion * 2.5`` when it is
        the victim), where ``weaponSpeedFactor`` is the weapon's speed *in
        seconds* times 3.5 and is doubled for a critical hit.  Only the
        auto-attack path rewards rage; special attacks (Heroic Strike and the
        rest) do not, matching ``Unit::DealDamage`` where only auto-attacks
        carry a ``cleanDamage->rageGained`` value.

        The result is stored in tenths of rage -- the unit ``UNIT_FIELD_POWER1``
        uses for rage -- so the client's rage bar and its "enough rage?" check
        agree with the server.  Rage is queued and flushed by the combat tick.
        """
        if not self.character or int(self.character.get("class", 1)) != 1:
            return
        damage = max(0, int(damage))
        if not damage:
            return
        level = max(1, int(self.character.get("level", 1)))
        if dealing:
            gain = attacker_rage_gain_tenths(
                damage, level, weapon_speed, crit
            )
        else:
            gain = victim_rage_gain_tenths(damage, level)
        if gain <= 0:
            return
        power = int(self.character.get("power", 0))
        self.character["power"] = max(0, min(self._max_power(), power + gain))
        self._queue_player_fields({self._power_field(F.UNIT_FIELD_POWER1): self.character["power"]})

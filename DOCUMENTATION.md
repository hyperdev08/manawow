# ManaWoW Documentation

## What works

A real 3.3.5a client can now log in, pick a character and **enter the world**:

### Authserver (realmd) on port `3724`
- `AUTH_LOGON_CHALLENGE` / `AUTH_LOGON_PROOF` with full SRP6
- Correct 40-byte session-key derivation and persistence
- 3.3.5a realm-list response

### Worldserver on port `8085`
- `SMSG_AUTH_CHALLENGE` (4-byte server challenge + 32-byte DOS challenge)
- `CMSG_AUTH_SESSION` parsing and digest validation
- HMAC-SHA1 + RC4-drop1024 encrypted packet headers
- `SMSG_AUTH_RESPONSE`, addon info, client-cache version, tutorial flags
- `SMSG_CHAR_ENUM`, `CMSG_CHAR_CREATE`, `CMSG_CHAR_DELETE`
- `CMSG_PLAYER_LOGIN` world-entry burst: `SMSG_LOGIN_VERIFY_WORLD`, account data times, feature status, MOTD, bind point, instance difficulty, initial spells/action buttons/factions, world states, time speed, the player's own create-object (`SMSG_COMPRESSED_UPDATE_OBJECT`) and time sync
- Login queries answered while in world: name query (including other accounts' characters), server time, played time, item query (unknown items) and calendar pending count
- Cross-account visibility: players entering the world receive create blocks for everyone already on the same map, existing clients are told about the newcomer, `MSG_MOVE_*` packets are relayed to other players, and `SMSG_DESTROY_OBJECT` is sent on logout or disconnect
- `CMSG_LOGOUT_REQUEST` / `CMSG_LOGOUT_CANCEL` back to character selection
- `CMSG_PING` / `SMSG_PONG`, realm split and keep-alive handling

### SQLite storage
- Split storage: `data/auth.db` (accounts/realms), `data/characters.db` (characters, inventory, spells, quest logs) and `data/world.db` (regenerable static content). None of the three is committed; the world bank is rebuilt from the pinned CMaNGOS dump
- `src/common/sqlite_tuning.py` (installed by the worldserver): WAL journal, `synchronous=NORMAL` and `wal_autocheckpoint=0` on every connection, plus a background thread that keeps one connection open and runs PASSIVE checkpoints. Rollback-journal commits used to freeze every session for seconds on Windows (median 362 ms, worst 16 s per commit)
- `src/common/db_io.py`: all hot writes run on one dedicated SQLite writer thread. Critical writes (position, inventory, money, XP, quests, loot) are awaited, so callers still read their own committed rows; creature health/power/aggro updates are queued and applied in execution order. Reads stay on the event loop and use WAL snapshots, which never wait for the writer. Hot read helpers share long-lived read connections (`sqlite_tuning.read_connection`), so they do not pay the cold connect/PRAGMA cost on every creature tick. No database write runs on the asyncio thread any more. Stop the server before copying or regenerating a database file so the WAL is folded back in

### Item system (initial milestone)
- `SMSG_ITEM_QUERY_SINGLE_RESPONSE` from `item_templates` SQLite table (all 44,613 CMaNGOS `item_template` rows; an older pre-import world database lacked 17,539 of them, which silently removed quest items such as 50432 Diseased Wolf Pelt from loot)
- Character inventory in `character_items` table (slot -> item instance)
- Starter kit: the exact `CharStartOutfit.dbc` outfit for the character's race/class (63 rows for the 62 legal 3.3.5a combinations, including the death-knight set and its four bags), with every referenced template seeded from the client's own `Item.dbc` data. Race/class legality comes from `CharBaseInfo.dbc`, so `CMSG_CHAR_CREATE` rejects impossible combinations (e.g., gnome paladin or tauren mage) with `CHAR_CREATE_FAILED`
- On world entry, items are emitted as `UPDATETYPE_CREATE_OBJECT2` blocks *before* the player block so the `PLAYER_FIELD_INV_SLOT_HEAD` / `PACK_SLOT_1` guids resolve immediately
- `PLAYER_VISIBLE_ITEM_*_ENTRYID` pairs for 19 slots for peers to see equipped gear
- `CMSG_SWAP_INV_ITEM` (equip slot <-> equip slot or backpack) persisted and followed by a player `UPDATETYPE_VALUES` update sent to the owner and every peer
- `CMSG_AUTOEQUIP_ITEM` (right-click to equip): the item's inventory type selects the destination slot
- `CMSG_DESTROYITEM` (delete from equipment or backpack) persisted
- `CMSG_SWAP_ITEM` accepted for the player container (bags 255/0)
- Unsupported moves receive `SMSG_INVENTORY_CHANGE_FAILURE` packets
- Weapon templates carry the sheath type (`sheath_type` column, hip/back/shield)
- `SMSG_CLIENTCACHE_VERSION` is a CRC32 of `item_templates`

### Static creatures + gossip (full 3.3.5a world data)
- `creature_templates` and `creature_spawns` SQLite tables plus `gossip_menus`, `gossip_options` and `npc_text`. `tools/import_wotlk_db.py` reads the canonical CMaNGOS `WoTLKDB` dump and fills them with the complete world creature data: 29,919 templates, 98,790 spawns, 4,331 gossip menus, 3,867 options and 5,620 NPC texts
- A fresh database seeds the eleven-NPC Northshire demo population (Marshal McBride, Deputy Willem, Innkeeper Farley, Michelle Belle and six critters/beasts) using display ids that exist in the client's `CreatureDisplayInfo.dbc`
- New characters start on the real 3.3.5a `playercreateinfo` position for their race/class (Elwynn, Durotar, Coldridge, Teldrassil, Tirisfal, Mulgore, Eversong, Azuremyst); death knights use the Ebon Hold start
- Creatures are sent as `TYPEID_UNIT` create blocks with `UPDATEFLAG_LIVING | UPDATEFLAG_HAS_POSITION` (0x60), movement info, nine speeds and a `UNIT_END` (148-field) update mask
- Creature guids use the `HIGHGUID_UNIT` pattern (0xF130 in the top 16 bits, entry in bits 24-47)
- Idle creatures wander around their spawn point or follow a waypoint path
- `CMSG_CREATURE_QUERY` -> `SMSG_CREATURE_QUERY_RESPONSE` with name, title, type/family/classification, four display ids
- `CMSG_GOSSIP_HELLO`, `CMSG_GOSSIP_SELECT_OPTION` for NPC menus
- `CMSG_NPC_TEXT_QUERY` -> `SMSG_NPC_TEXT_UPDATE`
- The world database ships invisible stalkers, mining/fire "bunny" helpers, quest-effect triggers, camera shakers, target dummies and the `[DND] TAR Pedestal` props
- Spawn visibility: each session keeps only a 150-yard neighbourhood, scanned using the `(map, x, y)` index and repeated after the player has moved 25 yards

### Combat + spells (real 3.3.5a layouts)
- `character_spells` stores the spells a character knows; each race/class combination gets the exact 3.3.5a level-1 spellbook from CMaNGOS `playercreateinfo_spell` (2919 rows across all 62 legal pairs), including racials, languages, armor and weapon proficiencies
- The default action bar comes from `playercreateinfo_action` (351 rows): Attack, the class's first ability, racials, and the second bar page; the default Attack macros become a normal Attack button because this server has no macro storage
- Class trainers teach the abilities that CMaNGOS does not grant in `playercreateinfo_spell` (for example `Summon Imp`, 688); the starter spellbook stays exactly as imported. `worldserver.start_money` mirrors CMaNGOS `StartPlayerMoney` (copper, default 0) so a new character can afford the first 10-copper trainer spell if the server increases it
- `SMSG_LOGIN_VERIFY_WORLD` is the first world packet, followed by `SMSG_INITIAL_SPELLS` and `SMSG_ACTION_BUTTONS`
- `CMSG_CAST_SPELL` / `CMSG_CANCEL_CAST` use imported `spell_templates`: cast time, range, power cost, GCD (`StartRecoveryTime`), school, damage/heal rolls and effect/aura enums
- `CMSG_CAST_SPELL` checks reagents, literal shaman totem items, `TotemCategory` tools and `RequiresSpellFocus` anvils/forges/fires before the cast starts; reagents and totems are consumed when the cast finishes, and `CREATE_ITEM`/`CREATE_ITEM_2` fail with an inventory error instead of destroying reagents when the product cannot be stored
- `SPELL_EFFECT_CREATE_ITEM`/`CREATE_ITEM_2`, `TRADE_SKILL`, `SKILL`, `LEARN_SPELL`, `SKILL_STEP` and `PROFICIENCY` are handled through the same item, trainer-skill and proficiency paths used by loot and trainers; a successful craft rolls the `SkillLineAbility.dbc` thresholds bundled in `src/world/skill_line_ability.sql` to advance the profession skill, and tool categories come from bundled `TotemCategory.dbc` rows
- Teleport effects 5/43/95 use `spell_target_position` and the same-map near-teleport or cross-map `SMSG_NEW_WORLD` path; trigger-spell/missile effects run their referenced spell with the existing triggered-cast pipeline
- Summon-object effects 76/104-107 create runtime gameobjects (traps, chests, ritual objects) for the owner and peers, expire with their spell duration and are removed on dismiss/logout; transmitted-object effect 50 creates its gameobject at the cast destination (campfires, rituals, scrying bowls) and spawn effect 46 is treated as the client-side no-op TrinityCore uses
- Activate-object 86 drives the object's open/close/use/destroy action, and gameobject-damage 87 tracks destructible-building health and sends the building-damage packet
- Quest-complete effects mark the active quest complete in the quest log; send-event effects dispatch through a server hook (`_spell_send_event`) so scripts can attach later
- Reactive `SPELL_AURA_PROC_TRIGGER_SPELL`, `SPELL_AURA_MOD_RATING` and `SPELL_AURA_MELEE_SLOW` are applied; rating auras feed the hit/crit/haste/dodge/parry/block/defense tables, and reactive procs use the attacker as the trigger target
- Spell-modifier auras 107/108 apply flat/percentage talents to matching spell-family masks for effect values, cast time, cost, range, cooldowns/GCD, critical chance and duration
- Force-cast effects 140/141/160 route their trigger spell to the selected target, and glyph effect 74 resolves `GlyphProperties.dbc` rows bundled in `src/world/glyph_properties.sql` and casts the glyph spell
- Party/raid area-aura effects 35/65 share the aura with same-map group members inside the spell radius; party auras in a raid stay in the caster's subgroup, raid auras cover the whole raid, and owner-only auras 143 stay on the caster. Friend/enemy/pet area-aura effects 128/129/119 apply to nearby players, hostile creatures and the active pet respectively
- Power Burn (62) drains the effect's power type and damages for the burned amount (Mana Burn's percentage cap included); Heal % (136) heals a percentage of maximum health; Jump Dest (42) moves the caster to the cast destination; Dispel Mechanic (108) strips auras of one mechanic; Inebriate (100) raises the drunk value with the 3.3.5 threshold packets; Charge (96) moves into melee range and starts the attack
- Kill-credit effects 90/134 award quest kill objectives for the effect's creature entry, and effect 134 also credits nearby group members
- Food/drink and regeneration auras 84/85/161 restore health/power over time (including in-combat Troll's Blood regeneration), while periodic 20/89 heal or damage a percentage of maximum health; drink spells copy their `PERIODIC_DUMMY` value into the regeneration aura, and drunk characters sober up one point every nine seconds
- Aura 61 scales the unit model, 142 applies the base-resistance percentage to agility-derived armor, 158 adds shield block value, and 30 adjusts skill values for trainer checks
- The talent system uses bundled `Talent.dbc`/`TalentTab.dbc` rows (`src/world/talents.sql`), grants one point per level from 10 to 80, validates tab class masks, tier point requirements and prerequisite ranks, learns the rank spell and applies passive talent auras, persists ranks in `character_talents`, and sends `SMSG_TALENTS_INFO` on login/learn/reset/level-up; `CMSG_LEARN_TALENT` and `CMSG_UNLEARN_TALENTS` are wired, with resets currently free and pet talents still pending
- Taunt (11) forces the creature's target for the aura duration; disarm (67) removes weapon damage and sets the disarm flags; proc-damage (43) and add-target-trigger (109) hooks fire from the aura/proc paths; force-reaction (139) sends `SMSG_SET_FORCED_REACTIONS`
- Mount auras 78/32/207 set `UNIT_FIELD_MOUNTDISPLAYID`, apply the mounted run-speed bonus and send flight-speed updates; stealth/invisibility 16/17/18/19 are tracked as modifier state for client auras
- Misc effects: Power Drain (8) drains power and energizes the caster, Leap (29) uses the cast destination, Jump (41) moves next to its target, Reputation (103) updates the character reputation map, Pull Towards (124) drags a target in front of the caster, and Energize % (137) restores a percentage of maximum power
- Learn Pet Spell (57) adds the triggered spell to the active pet's spell list and action bar, Create Tamed Pet (153) summons the entry as a permanent pet, Sanctuary (79) leaves combat and clears threat, Add Honor (45) updates the honor pool, and Unlearn Specialization (133) removes the trigger spell from the spellbook and sends `SMSG_REMOVED_SPELL`
- `python tools/spell_coverage.py` audits every imported spell against the implemented effect/aura families and prints unsupported families with example spells; the current dataset has 50,889 templates and 37,886 fully covered spells
- Melee auto-attack, weapon damage/delay, creature aggro + chase + leash AI (a creature evades when its victim is more than 60 yards from the spawn, `Creature::CanCreatureAttack`; its own position does not count, so low-health humanoids can flee without resetting), creature melee, death, respawn, XP and level-up
- `SMSG_SPELL_START` / `SMSG_SPELL_GO` / `SMSG_CAST_FAILED` with miss/crit flags
- `SMSG_SPELLHEALLOG`, `SMSG_SPELLENERGIZELOG`, `SMSG_LOG_XPGAIN` and `SMSG_LEVELUP_INFO`
- Rage is stored in tenths (full bar = 1000) and generated with the real 3.3.5 formula; out of combat rage decays 25 points every two seconds
- Spell power costs follow `Spell::CalculatePowerCost`
- Energy regenerates 10 per second for rogues
- Mana uses the client's `GtRegenMPPerSpt.dbc` ratio: `sqrt(Intellect) * Spirit * ratio[level]` per second
- Rogue combo points: per-target state pushed with `SMSG_UPDATE_COMBO_POINTS`, max 5
- Starting attributes come from imported CMaNGOS `player_levelstats` rows
- Armed items add primary stats, flat health/mana, attack power and resistances, mirroring `Player::_ApplyItemMods`
- Creature melee is mitigated by armor with `Unit::CalcArmorReducedDamage`
- Melee swings use a real hit table: miss, dodge, parry, glancing, block, crushing and crit, modified by the attacker's hit/crit/expertise ratings, the player's dodge/parry/block/resilience and level difference; dual wield adds the off-hand swing with its 15% extra miss chance and 50% damage
- Armor penetration reduces creature armor; creature attack power (and AP auras) feeds creature damage
- Ranged auto-attack (Auto Shot/Shoot/wand) uses the ranged slot's weapon damage, ranged attack power, ranged haste and the same hit/crit table, with a 41-yard maximum range
- Melee, ranged and hostile spells check facing: weapon abilities require the victim in front, `FacingCasterFlags=1` spells require it too, and melee, ranged attacks and casts also check line of sight (`SMSG_ATTACKSWING_BADFACING` / `CANT_ATTACK` / `SPELL_FAILED_LINE_OF_SIGHT`)
- Line of sight reads the extracted 3.3.5a `.map` terrain heightmap/holes and `.vmap` static model collision; the optional `sight_blockers` table remains as a hand-authored AABB supplement, and an empty client-data directory falls back to the old "clear world" behaviour
- Item on-hit procs use the real `item_template` chance-on-hit trigger and `spellppmRate_*`: PPM is normalized against the weapon's base speed, with per-item cooldowns/charges
- Temporary weapon enchants and poisons are read from bundled `SpellItemEnchantment.dbc` data: poison/stone application stores the enchant per weapon slot, persists it in `character_items.enchantment_2`, and procs on auto-attacks and weapon abilities; `CMSG_CANCEL_TEMP_ENCHANTMENT` clears it
- Poison rates match 3.3.5: Instant Poison 8.57 PPM, Wound Poison 21.4 PPM, Deadly Poison 30%, Crippling/Mind-numbing 50%, with half chance from the off-hand
- Spell casts use the 3.3.5 hit/miss and partial-resistance rules: 4% base miss at equal level modified by spell hit rating and level difference, spell crit rating, per-school resistances reduced by spell penetration, absorb shields, school/mechanic/effect immunities and spell reflection
- Direct damage/heals scale with spell power/healing power using the DBC bonus coefficient; heals crit for 1.5x and damage for 2x
- Damage taken while casting pushes the cast back (up to two 0.25-0.5 s delays) and shortens an active channel; moving out of range cancels the cast/channel
- Out-of-combat health regeneration uses the spirit/level formula instead of a flat percentage
- Death knight runes are real state: blood, blood, unholy, unholy, frost, frost, regenerated at 0.1 per second
- Area-of-effect spells use the real 3.3.5 `EffectImplicitTarget`/`SpellRadius.dbc` data:
  - self-centred areas (Arcane Explosion, Thunder Clap, Frost Nova, Whirlwind, Blood Boil, Consecration) select every enemy within the effect radius
  - ground-targeted areas (Flamestrike, Blizzard, Rain of Fire, Volley, Earthquake, Death and Decay) read the click destination from the cast packet and place the effect there
  - 90-degree cones (Cone of Cold, Shockwave) check the caster's facing
  - chain spells (Chain Lightning, Multi-Shot) jump through up to `EffectChainTargets` nearby enemies with the reference 30% falloff per jump
  - persistent ground effects tick from `SpellDuration.dbc` and take their damage from the periodic aura or its `PERIODIC_TRIGGER_SPELL` (e.g. Blizzard 42208, Rain of Fire 42223)
  - `MaxTargets` is honoured (Whirlwind hits four), the caster's own pet is excluded, and creature AoE (Flamestrike) damages the player and leaves a hostile ground effect
  - party/raid-only heals and buffs are applied to the caster and to every group member on the same map inside the area; a solo caster only affects themselves
  - ground effects with a slow component (Blizzard, Hurricane, ...) also refresh a short movement-slow aura on each hit target

### Party and raid
- `CMSG_GROUP_INVITE` / accept / decline / uninvite / leave / disband / set leader / raid convert / subgroup change and swap
- `SMSG_GROUP_LIST`, `SMSG_GROUP_INVITE`, `SMSG_GROUP_DECLINE`, `SMSG_GROUP_UNINVITE`, `SMSG_GROUP_SET_LEADER`, `SMSG_GROUP_DESTROYED`, `SMSG_PARTY_COMMAND_RESULT`
- Party member frames receive periodic `SMSG_PARTY_MEMBER_STATS_FULL` updates (health, power, level, zone, position)
- Party/raid chat is relayed only to group members; cross-faction invites are rejected
- Groups are 5-player parties or 40-player raids with eight subgroups; the leader is transferred when the current leader leaves
- Groups and their members are persisted in `groups` / `group_members` in the character database. A logout keeps the member in the group as offline (`MEMBER_STATUS_OFFLINE`), and both the group and its settings are restored after a server restart or when a member logs back in
- If every member stays offline for the reconnect window, the group is automatically disbanded. The default is five minutes (`worldserver.group_offline_expiry`, seconds; `0` disables it); a reconnect inside the window clears the timer and restores the group
- Loot method (`CMSG_LOOT_METHOD`) follows the 3.3.5a enum: free-for-all, round robin, master loot, group loot and need-before-greed, with a configurable loot threshold (uncommon..artifact). The settings are stored with the group and shown in `SMSG_GROUP_LIST`
- Group loot / need-before-greed start 60-second need/greed/pass rolls for items at or above the threshold (`SMSG_LOOT_START_ROLL`, `SMSG_LOOT_ROLL` roll results, `SMSG_LOOT_ROLL_WON`, `SMSG_LOOT_ALL_PASSED`); silent members pass when the timer expires, and an all-pass leaves the item lootable by hand
- Master loot blocks above-threshold items for everyone except the master looter, sends `SMSG_LOOT_MASTER_LIST`, and distributes slots through `CMSG_LOOT_MASTER_GIVE`
- `CMSG_OPT_OUT_OF_LOOT` makes a member automatically pass on future group rolls
- Ready checks (`MSG_RAID_READY_CHECK`, `MSG_RAID_READY_CHECK_CONFIRM`, `MSG_RAID_READY_CHECK_FINISHED`) are leader/assistant-driven and time out after 30 seconds
- Raid target icons (`MSG_RAID_TARGET_UPDATE`) support all eight markers, leader/assistant restriction in raids, duplicate-marker cleanup and persistence
- Group XP is split among nearby living members on the same map with a 10% bonus per extra member (capped at 1.5x)
- Loot rights belong to the tagger's group; only one member can have a corpse's loot window open at a time to prevent duplicate item takes
- Limitations: round-robin looter rotation, disenchant rolls and LFG are not implemented

### Death, ghosts, corpses and spirit healers
- On death the server stores a corpse at the death position, removes 10% durability from every equipped item (persisted and sent to the client with `SMSG_DURABILITY_DAMAGE_DEATH`), records `death_state` (`1` dead, `2` ghost) and sends `SMSG_DEATH_RELEASE_LOC` with the nearest usable graveyard.
- Graveyards come from the CMaNGOS `world_safe_locs` and `game_graveyard_zone` tables, imported by `tools/import_wotlk_db.py` (including `--graveyards-only`). Selection mirrors `Player::GetClosestGraveYard`: zone + team first, then neutral zone links, then the nearest same-map graveyard.
- `CMSG_REPOP_REQUEST` teleports the player to that graveyard as a ghost: `PLAYER_FLAGS_GHOST`, one health point, `SMSG_PRE_RESURRECT`, `SMSG_CORPSE_RECLAIM_DELAY` and the usual worldport handshake. The ghost can walk around but cannot fight, loot or cast.
- `CMSG_RECLAIM_CORPSE` resurrects at the corpse (same map within 39 yards, or at the instance entrance graveyard for a cross-map corpse) after the 30-second reclaim delay, at 50% health/mana. `CMSG_SPIRIT_HEALER_ACTIVATE` (or the spirit healer gossip option) resurrects at the graveyard with 50% health/mana, 25% durability loss and Resurrection Sickness (no sickness below level 11, one minute per level from 11–19, ten minutes from 20).
- `MSG_CORPSE_QUERY` returns the corpse's `(map, x, y, z)`, and `SMSG_DEATH_RELEASE_LOC` is cleared with map `-1` on resurrection.
- The corpse is also a real `TYPEID_CORPSE` object (`HighGuid::Corpse`, `CORPSE_FIELD_*`): it is broadcast to other players in range, appears when they log in or walk nearby, and is destroyed on resurrection.
- Cross-map instance corpses can be reclaimed while standing at the corpse's entrance graveyard; the server switches to the corpse map through the normal `SMSG_NEW_WORLD` worldport handshake.
- The ghost flag and corpse survive logout/relogin; the character list marks ghosts with `CHARACTER_FLAG_GHOST`.
- Limitations: no PvP corpse reclaim tiers (30/60/120 s), and battleground spirit guides/resurrect queues are not implemented.

### Auras (buffs, debuffs, control and mitigation)
- A real aura registry for players (`combat["auras"]`) and creatures (`combat["creature_auras"]`), keyed by spell so a recast refreshes the duration and stacks up to `Spell.dbc` `StackAmount`
- Components implemented: periodic damage/heal/leech/energize, stun, root, fear, confuse, charm, silence, pacify, sleep/polymorph/banish/shackle mechanics, movement increase/decrease, primary stats and percentage/total-stat modifiers, health/energy (flat and percent), armor/resistances (flat and percent), attack power/ranged AP (flat and percent), spell/healing power and percent, melee/ranged/spell haste, hit/crit/dodge/parry/block ratings, damage-done and damage-taken modifiers, damage shields/Thorns, spell reflection, school/mechanic/effect/dispel immunities and school absorb shields
- Durations come from `SpellDuration.dbc` (full 3.3.5 fallback table); expired auras remove their modifiers, send a removal `SMSG_AURA_UPDATE` and resend the affected fields
- Every applied/refreshed/removed aura is sent with `SMSG_AURA_UPDATE`: 3.3.5 slot, spell id, flags (effect bits, self-cast/negative/duration), caster level, stacks, optional caster guid and max/remaining duration; creature auras are sent too
- Player movement: `SMSG_FORCE_RUN_SPEED_CHANGE`, `SMSG_FORCE_MOVE_ROOT` / `SMSG_FORCE_MOVE_UNROOT`; stun/fear/confuse/silence/pacify set the corresponding `UNIT_FIELD_FLAGS` bits
- Creature movement: slows scale chase/idle speed, roots stop chasing and idle wandering, stuns stop the AI entirely, fear/confuse make the creature run away, silence blocks casts and pacify blocks melee swings
- Crowd control uses a per-target diminishing-returns ladder (100% / 50% / 25% / immune) with the 15-second reset, grouped by stun, root, fear, polymorph, charm, sleep and silence mechanics
- Polymorph/sleep/disorient break when the victim takes damage; charm and mind-control currently stop the victim's AI/casting but do not transfer client control or a pet bar
- Absorb shields (Power Word: Shield, Ice Barrier, ...) drain before health and are removed when depleted; spell reflection sends the 3.3.5 reflect miss; dispel/steal effects remove matching auras and send `SMSG_SPELLDISPELLOG` / `SMSG_SPELLSTEALLOG`
- Player melee damage includes the `attack_power / 14 * weapon speed` contribution, so Battle Shout and stat auras change real swings
- Periodic auras refresh instead of stacking duplicate DoTs and use their real duration (Fireball's 4 s burn instead of a flat 15 s)
- Auras are session-scoped, matching the reference server's behaviour for temporary buffs (they do not survive logout); passive/area aura persistence is not modelled yet

### Summoning (pets and guardians)
- `SPELL_EFFECT_SUMMON_PET` (56, e.g. Summon Imp/Voidwalker/Succubus/Felhunter) and `SPELL_EFFECT_SUMMON` (28, guardians such as Mirror Image) create a session-local creature next to the caster
- The summon is sent as a normal `TYPEID_UNIT` create block with `UNIT_FIELD_SUMMONEDBY`/`UNIT_FIELD_CREATEDBY` (owner guid, indices 14-17), `UNIT_FLAG_PLAYER_CONTROLLED` and the owner's faction template, so the client draws it as the player's pet
- Create/move/destroy blocks are relayed to other players on the same map; a player who enters later receives the existing pet's create block as well
- The pet follows its owner when farther than 4 yards, chases enemies the owner is fighting (leash 40 yards) and swings on a 2 s timer into the same `SMSG_ATTACKERSTATEUPDATE` / damage path as creature melee
- A second summon replaces the first (warlock demons dismiss the old pet when the replacement finishes); logout and disconnect remove the pet for every client
- Summon duration comes from `SpellDuration.dbc`: index 21 (`-1`, until dismissed) covers warlock pets, index 0 is treated as permanent, and the table has fallbacks for the common timed guardians until `--spell-duration-dbc` is imported
- `Summon Imp` is part of the level-1 warlock starter kit, matching the real class; Call Pet (883) has no stored-pet state and is refused instead of summoning nothing
- Summoned gameobjects (`SPELL_EFFECT_SUMMON_OBJECT_WILD` 76 / `SUMMON_OBJECT_SLOT1` 104, e.g. traps) are recognised but ignored because the server has no gameobject model
- `SMSG_PET_SPELLS` builds the 3.3.5 pet action bar with attack/follow/stay, command/reaction buttons and the pet's learned spells; `CMSG_PET_ACTION` implements attack, follow, stay, passive/defensive/aggressive and dismiss, and `CMSG_PET_SET_ACTION` supports moving or clearing bar buttons
- Pet spell autocast state is toggled through `CMSG_PET_SPELL_AUTOCAST` (and the action-bar spell states), persisted for the lifetime of the summon and resent in `SMSG_PET_SPELLS`; pet spell casting itself still uses the melee AI
- `CMSG_PET_RENAME`/`CMSG_PET_NAME_QUERY` support renaming the active summon (including the `UNIT_FIELD_BYTES_2` rename flag); stables and hunter-pet persistence are still not implemented

### Creature spell-list AI
- Creatures use database spell lists during combat
- 1200 ms tick with `Availability` rolled once when the list is assigned
- `Probability` as a selection weight, `InitialMin/Max` and `RepeatMin/Max` in milliseconds
- Target ids below 100 use hardcoded rules; ids 100+ resolve through `creature_spell_targeting`
- Creatures with `SpellList = 0` fall back to legacy `creature_template_spells` plus `creature_cooldowns`
- `SPELL_ATTR_EX_NO_AUTOCAST_AI` (`0x00020000`) spells are skipped
- Creature casts pay mana, observe cooldowns/categories and regenerate mana in combat; an interrupt effect cancels the cast and locks the spell school for four seconds
- Creature spells use the same immunity/reflect/absorb/resistance path as player spells, and apply the same auras/DoTs to the player
- Creature aggro/threat/target/position/auras/idle state is world-shared: all sessions attach the same dictionaries, the highest-threat living player becomes the single AI driver, and damage from other players/pets is attributed to its owner
- Threat switching uses the 110% melee / 130% ranged rules, healing generates half-effective-heal threat, threat modifiers and taunt effects (Taunt, Growl, Mocking Blow) update the shared table
- Nearby same-faction creatures are called for help when a fight starts
- Loot/XP goes to the first tagger (pet damage credits the owner); the corpse stores `loot_owner`, so other solo players cannot open it
- Misdirection/Tricks-style threat redirect is supported; threat wipes/vanish/encounter-specific threat mechanics are not
- Low-health humanoid creatures flee for six seconds instead of fighting to the death
- `SPELL_EFFECT_ACTIVATE_RUNE` (146) not implemented
- Creature casts and melee respect the same facing/LOS rules; chase, flee, wander and waypoint movement use the extracted navigation mesh when a straight line is obstructed
- Creature summons/totems and ranged creature auto-attacks are not modelled yet

### Idle creature movement
- Wander: random navmesh destination within `spawndist` radius, pause 3-10 s, walk speed, max 3 consecutive moves
- Waypoints: follow `creature_movement`/`creature_movement_template` node by node
- Obstacles: direct movement is used while the straight segment is clear; otherwise the waypoints are routed through the navigation mesh
- Runtime selection: spawn `movement_type` >=0 overrides template; 0=idle, 1=wander, 2=waypoint; -1 uses path if present, NPC units idle, wild creatures wander

### Terrain, line of sight and wayfinding
- Client collision data lives under `worldserver.client_data` (default `./data/client`) and is **not** committed because the full 3.3.5a extract is several gigabytes. Populate it with:
  - `python tools/fetch_client_data.py --preset starter` for Northshire/Elwynn (map 0, tile 48,32 +/- 2)
  - `--map N --tile X,Y`, `--map N --around X,Y --radius N`, or `--map N --all-map`
  - `--source` selects another mirror with the same layout; the default is the public `Torrer/TrinityCore-3.3.5-data` 3.3.5a extract
- The server never downloads data at runtime. If the directory is empty or a tile is missing, LOS returns clear and pathfinding falls back to the previous straight-line movement.
- Terrain (`src/world/terrain.py`) reads `maps/MMMXXYY.map`: float/int16/int8 heightmaps, current and legacy (CMaNGOS 512-byte) holes, and exposes ground height plus sampled segment occlusion. This catches hills, cliffs and cave holes even when both ray endpoints are in the air.
- Static world models (`src/world/vmap.py`) read `vmaps/MMM_YY_XX.vmtile` placements, global WMOs from non-tiled `MMM.vmtree` maps, and `vmaps/<model>.vmo` collision groups. WMOs test their real collision triangles through a lazily built BVH, which preserves doorways and interior walls; if a `.vmo` is missing they fall back to the transformed per-group AABB. M2 placements use their transformed model bound. The extractor's reflected placement space (`17066.666 - x/y`) is converted back to world coordinates.
- Wayfinding (`src/world/navmesh.py`) reads the Detour `mmaps/MMMXXYY.mmtile` polygon mesh, converts Detour's Y-up vertices back to WoW coordinates, links internal `neis`, cross-tile shared portal edges and off-mesh connections, and runs A* over polygon centres. Detail sub-meshes are read too, so `point_on_mesh`/`nearest_poly` heights follow stairs and uneven WMO floors instead of a flat polygon plane.
- The corridor is converted to a taut path with Detour's funnel/string-pulling algorithm; off-mesh links contribute their actual jump endpoints. Area/flags from the tile (ground, steep ground, water, magma/slime) are filtered through TrinityCore's `NavTerrainFlag`; movement queries default to ground/steep and only include water when the caller explicitly asks for `NAV_WATER`.
- Chase and flee only pay for an A* query when the straight line is obstructed. Summons, waypoint movement, wander and the evade walk-home path use the same helper; a multi-point spline carries all waypoints in one packet.
- Tile caches are bounded (64 navmesh tiles, 64 VMap tiles, 48 WMO models) so exploring the world does not grow memory without limit. If the first A* attempt only has the endpoint tiles, the direct start-to-end corridor is loaded and the search is retried; endpoint/corridor tiles are kept in the LRU so the cache cannot evict the path while it is being built. Routes longer than the cache are solved in overlapping eight-tile chunks, releasing each chunk before loading the next.
- Each parsed tile carries a 16-yard spatial hash of its walkable polygon indices. `nearest_poly`/`point_on_mesh` only inspect the cells overlapping the query instead of walking every polygon of every loaded tile, which cut warm point/random/wander lookups from tens of milliseconds to well under one.
- If a navmesh tile exists at the unit's position but no corridor can be generated, the creature holds position instead of faking a straight spline through the obstacle. Maps or areas without generated navmesh keep the previous straight-line fallback.
- Navmesh tile parsing, polygon-graph rebuilds, A* and `random_point`/`nearest_poly` scans run on a dedicated geometry thread (`src/common/geometry_io.py`), serialised so the lazily built mesh cache stays consistent. The asyncio loop only receives the finished spline.
- Limitations: only static VMap geometry blocks sight (dynamic doors/gameobjects are not included); there is no creature jump/climb animation selection; terrain LOS is sampled at 0.5 yards; and player movement remains client-authoritative (no server-side collision correction).

### Loot
- `creature_loot_template` (218,066 rows) and `reference_loot_template` (44,244 rows / 896 pools) imported from CMaNGOS
- Loot is rolled exactly once when the creature dies, stored in `creature_spawns.loot_json`; the first tagger is recorded in `creature_spawns.loot_owner`
- Roll rules follow CMaNGOS: references use `mincountOrRef` multiplicities, groups shuffle explicit-chance rows and use equal-chance fallbacks, conditional rows are skipped until a condition engine exists, and the window is capped at 16 slots
- Corpse money uses the real `creature_template.MinLootGold` / `MaxLootGold` values imported from CMaNGOS (no synthetic level-based coins)
- `CMSG_LOOT`, `CMSG_AUTOSTORE_LOOT_ITEM`, `CMSG_LOOT_MONEY`, `CMSG_LOOT_RELEASE`
- Quest items (negative `ChanceOrQuestChance`) only drop for a player with an active, unfinished quest that needs them
- Drop chances are stored as floats (`0.5`, `6.4011`...); refresh the loot tables of an older `world.db` with `python tools/import_wotlk_db.py --loot-only`

### Quests
- `quest_templates` (9,086 quests), `creature_quest_starters` / `creature_quest_enders` (from `creature_questrelation` / `creature_involvedrelation`) and `areatrigger_quests` (from `areatrigger_involvedrelation`) imported from CMaNGOS; an existing `world.db` gets them with `python tools/import_wotlk_db.py --quests-only`
- Rules and packet builders live in `src/world/quests.py` (socket-free), handlers in `src/world/worldsocket/quests.py`
- Quest giver markers: `CMSG_QUESTGIVER_STATUS_QUERY` / `CMSG_QUESTGIVER_STATUS_MULTIPLE_QUERY` answer with the 3.3.5 dialog status (yellow/grey "!" and "?"); statuses are re-sent after accepting, abandoning, progressing or turning in a quest
- `CMSG_QUESTGIVER_HELLO` and `CMSG_GOSSIP_HELLO`: quests are appended to the gossip window; a giver whose menu has no options opens the quest list, or the single quest directly (`Player::SendPreparedQuest`)
- `QUEST_FLAGS_AUTO_ACCEPT` (0x80000) is stripped from every quest packet. The CMaNGOS dump sets it on all 178 starting-zone quests, but no 3.3.5a quest had it on retail (TrinityCore `QuestDef.h`); the client would turn Accept into a plain close button and never send `CMSG_QUESTGIVER_ACCEPT_QUEST`
- `CMSG_QUESTGIVER_QUERY_QUEST` -> `SMSG_QUESTGIVER_QUEST_DETAILS`, `CMSG_QUESTGIVER_ACCEPT_QUEST`, `CMSG_QUEST_QUERY` -> `SMSG_QUEST_QUERY_RESPONSE`, `CMSG_QUESTLOG_REMOVE_QUEST`, `CMSG_QUESTGIVER_CANCEL`
- Taking a quest checks race/class masks, level, `PrevQuestId` and quests pointing at it through `NextQuestId`, positive `ExclusiveGroup`, already-done (unless repeatable) and the 25-slot log; `SrcItemId` is handed out on accept and taken back on abandon
- The quest log is stored per character in `character_quests` (slot, status, explored flag, four kill counters) and `character_quests_rewarded`, and mirrored in the `PLAYER_QUEST_LOG_*` update fields (sent in the create block on login)
- Objectives: creature kills (including `KillCredit1/2`) send `SMSG_QUESTUPDATE_ADD_KILL`; item objectives are counted from the bags, so looting, destroying or handing in items re-evaluates the quest; exploration quests complete on `CMSG_AREATRIGGER`; `SMSG_QUESTUPDATE_COMPLETE` marks a finished objective set
- Turn in: `CMSG_QUESTGIVER_COMPLETE_QUEST` -> `SMSG_QUESTGIVER_REQUEST_ITEMS` (or straight to the reward), `CMSG_QUESTGIVER_REQUEST_REWARD` -> `SMSG_QUESTGIVER_OFFER_REWARD`, `CMSG_QUESTGIVER_CHOOSE_REWARD` takes the required items/money, gives fixed + chosen reward items, money and XP and sends `SMSG_QUESTGIVER_QUEST_COMPLETE`; the next quest of the chain opens right away when the same NPC starts it
- Quest XP is derived from `RewMoneyMaxLevel` like CMaNGOS (QuestXP.dbc is not in the SQL dump); at level 80 the quest pays that money instead
- `CMSG_QUEST_POI_QUERY` is answered with empty POI lists
- Not implemented yet: game object starters/enders and objectives, quest-starting items, timed and escort/event quests, reputation/skill/condition requirements, reputation, spell and title rewards, quest sharing, quest greeting texts and daily/weekly resets

### Vendors
- `npc_vendor` (35,910 rows for 1,982 vendors) imported from CMaNGOS `npc_vendor` plus the shared `npc_vendor_template` lists, and `item_templates.buy_count` (water and food come in bundles of 5, ammo in 200s)
- `CMSG_LIST_INVENTORY` -> `SMSG_LIST_INVENTORY`; gossip vendors open the list straight away, or get a "browse your goods" option added to their menu; CMaNGOS gossip options of type vendor open it too; a vendor that also gives quests shows the quests next to the "browse your goods" option (`Player::SendPreparedGossip`)
- `CMSG_BUY_ITEM` / `CMSG_BUY_ITEM_IN_SLOT` -> `SMSG_BUY_ITEM` / `SMSG_BUY_FAILED` (not enough money, sold out, wrong slot, too far); items go to the backpack and stack with copies already there
- Limited stock (`maxcount`/`incrtime`) per spawn, refilled after `incrtime` seconds
- `CMSG_SELL_ITEM` for backpack items at `sell_price`, whole or partial stacks
- Hidden from the list: other-faction items and bind-on-pickup gear for other classes
- Not implemented yet: honor/token prices (`ExtendedCost`, those items are hidden), buyback, repair, reputation discounts, `condition_id`
- Upgrade an existing `world.db` with `python tools/import_wotlk_db.py --vendors-only`

### Spell trainers
- `npc_trainer` (18,146 rows), shared `npc_trainer_template` (2,812 rows), `trainer_greeting` (698 rows) and the `TrainerType`/`TrainerSpell`/`TrainerClass`/`TrainerRace`/`TrainerTemplateId` columns of `creature_templates` are imported from CMaNGOS. All four trainer types are handled: class, mount, tradeskill and pet trainers, including the shared template lists used by 421 creatures
- `CMSG_GOSSIP_HELLO` exposes the CMaNGOS "Train me." option (type 5) only when `Creature::IsTrainerOf` passes: matching class for class trainers, hunter for pet trainers, matching race for mount trainers and the required specialization spell for tradeskill trainers. `CMSG_TRAINER_LIST` works directly as well
- `SMSG_TRAINER_LIST` uses the 3.3.5a 38-byte row layout (spell, green/red/gray state, cost, primary-profession flag, required level/skill/value and three required abilities), the CMaNGOS greeting text and the list-derived trainer type
- `CMSG_TRAINER_BUY_SPELL` validates the row, state, level, skill, ability and rank-chain prerequisites, charges the listed copper cost, sends the trainer/player visual effects, `SMSG_LEARNED_SPELL`/`SMSG_SUPERCEDED_SPELL` and `SMSG_TRAINER_BUY_SUCCEEDED`. Profession and riding wrappers (`SPELL_EFFECT_LEARN_SPELL` plus `SPELL_EFFECT_SKILL_STEP`) learn the real spell and update the character's skill line (Apprentice 75 ... Grand Master 450), persisted in `character_skills` and included in the player skill array. Weapon/armour proficiency spells send and persist the merged mask (`character_proficiencies`), re-sent on every login
- Not implemented yet: reputation price discounts, the two-primary-profession slot limit, GM trainer overrides, `condition_id` rows and pet-talent training (`TRAINER_TYPE_PETS` has no spell rows in CMaNGOS; the talent window is separate)
- Upgrade an existing `world.db` with `python tools/import_wotlk_trainers.py`

### Game objects
- `gameobject_templates` (19,555) and `gameobject_spawns` (100,337) imported from CMaNGOS `gameobject_template` / `gameobject`, with `gameobject_addon` start states and animation progress and `gameobject_spawn_entry` for spawns without a fixed entry
- `0xF110` object guids with the entry in bits 24-47; create blocks with stationary position, low guid and the packed int64 world rotation (TrinityCore `GameObject::UpdatePackedRotation`); doors and buttons with `startOpen` start open
- `CMSG_GAMEOBJECT_QUERY` -> `SMSG_GAMEOBJECT_QUERY_RESPONSE` (name, icon, cast-bar caption, the 24 data fields, size, quest items)
- Objects within 150 yards come with the login burst and are created/destroyed as the player moves; each map's spawns are read once, off the event loop, into an in-memory 64-yard grid, so visibility scans never query SQLite
- `CMSG_GAMEOBJ_USE`: doors and buttons open/close for every player (with their auto-close timer); chests open their loot; chairs and benches seat the player on the nearest slot (same-map teleport + sit stand state); books and plaques open their pages (`SMSG_GAMEOBJECT_PAGETEXT` + `CMSG_PAGE_TEXT_QUERY`, 1,879 `page_text` rows); cameras start their cinematic; mailboxes open empty (no mail system yet)
- Locked objects are opened by the client's spell (Opening, Herb Gathering, Mining...): `SPELL_EFFECT_OPEN_LOCK` on a game object target; the client checks the lock's skill against its own `Lock.dbc`
- Object loot comes from `gameobject_loot_template` (15,862 rows) plus the template's min/max gold; consumable chests, herbs and ore despawn when emptied and respawn after `spawntimesecs`, reusable chests refill after their restock time
- Quest chests are clickable and sparkle (`GAMEOBJECT_DYNAMIC`) only for players who need them
- CMaNGOS pools (`pool_gameobject`, `pool_gameobject_template`, `pool_template`, `pool_pool`): only `max_limit` members of a pool exist at a time, and a used one is replaced by a new roll after its respawn time (29,763 pooled spawns)
- Not shown: transports (elevators, boats) and spawns with a negative respawn time (script-only)
- Quest objects: wanted posters and other QUESTGIVER objects start/end quests (`gameobject_questrelation` / `gameobject_involvedrelation`: 527 starters, 443 enders) through the usual quest windows; they are clickable (`GO_DYNFLAG_LO_ACTIVATE`) only while they have something for the player, re-checked on quest changes and level-up. The 3.3.5 client shows no "!" over objects and never queries their status
- Goobers: page text, quest gating (`goober.questId`), "use the object" objectives (negative `ReqCreatureOrGOId`, credited like `Player::KillCreditGO`), custom animation or open state with auto-close, on-use spell, consumable despawn; quest goobers sparkle for players on the quest, generic quest objects only sparkle
- Quest-starting items (`item_template.startquest`, 385 items): `CMSG_USE_ITEM` opens the quest; the item is used up on accept unless the quest asks for it. Other item use (potions, food, hearthstone) is item spell casting (#11)
- CMaNGOS `spawn_group` limits (`MaxCount`, groups without a `WorldState` condition) are imported as pools for both objects and creatures
- Not usable yet: traps, spell casters, meeting stones, fishing, summoned objects
- Upgrade an existing `world.db` with `python tools/import_wotlk_db.py --gameobjects-only`

### Creature pools
- `pool_creature`, `pool_creature_template` and `spawn_group` (with `MaxCount`, no `WorldState`) limit how many spawns of a group exist at once: rare spawns such as Gruff Swiftbite or Fedfennel show at one of their spawn points instead of all of them (715 pooled creatures)
- Pools are loaded once per server, off the event loop, and filter the creature visibility scans
- Refresh with `python tools/import_wotlk_db.py --pools-only` (creature and object pools)
- Spawns stored with `id = 0` take their entry from `creature_spawn_entry` / `gameobject_spawn_entry` or, inside a spawn group, from `spawn_group_entry` (chosen once per guid at import, so it is reproducible): 5,650 creatures and 3,136 objects that used to be skipped. Add them to an existing `world.db` with `python tools/import_wotlk_db.py --spawn-entries-only` (creatures; `--gameobjects-only` for objects)
- Not handled yet: `spawn_group` world-state conditions (day/night RP groups keep spawning)

### World events
- There is no event calendar yet, so the world is shown with no world event active: creatures and objects that only spawn during an event (`game_event_creature` / `game_event_gameobject` with a positive event: Darkmoon Faire, Midsummer, Hallow's End, Winter Veil, the Arena Tournament realm NPCs...) are hidden; spawns removed during an event (negative event) stay
- The links are stored in `creature_events` / `gameobject_events`; refresh them with `python tools/import_wotlk_db.py --events-only`

### Chat + emotes
- `CMSG_MESSAGECHAT` / `SMSG_MESSAGECHAT`: `/say`, `/yell`, `/whisper`, `/me` (emote)
- `CMSG_TEXT_EMOTE`, `CMSG_EMOTE`, `CMSG_SET_SHEATHED`

### Cross-account relay
Two accounts can see each other spawn, move, and despawn, with equipment updates and chat relayed between them.

## Quick setup

### Required tools

```bash
python tools/create_test_account.py TEST TEST
```

### Starting the servers

```bash
# Combined (recommended):
python run_servers.py

# Or separately:
python run_auth.py
python run_world.py
```

### Finding event-loop stalls

Every session runs on one asyncio loop, so a blocking call freezes all players at once. Start the servers with the stall watchdog to find them:

```bash
python run_servers.py --watchdog        # report stalls of 1 s or more
python run_servers.py --watchdog 0.25   # or pick the threshold
```

Each stall is appended to `logs/stalls.log` with the stack of the loop thread at that moment (the line that is blocking, repeated every 2 s while it lasts) and its total duration once the loop answers again. The worldserver log gets a one-line warning per stall. `run_world.py` takes the same flag.

Combat and movement hot paths avoid blocking SQLite on the loop thread: spell templates/durations, creature spell lists, cooldowns and targeting rows are cached (`spells.clear_caches()` drops them for tests or a data re-import), weapon stats and on-hit item procs are cached per session, and the client-cache version aggregate is computed once off-loop. Weapon stats, line-of-sight/terrain/VMap checks run on the dedicated geometry worker, `sight_blockers` is loaded once instead of statting the database per query, and the `asyncio` transport logger writes to the normal log file instead of `logging.lastResort` on the console.

Ambient creature movement is spread over ticks: a rotating cursor lets each combat tick spend at most `IDLE_GEOMETRY_MOVES_PER_TICK` pathfinding/LOS lookups, so a crowd of wandering NPCs can no longer delay swings and chase updates. A failure in one creature's idle or combat tick is logged and isolated instead of aborting the rest of that tick. The respawn scan is one bulk query (run off-loop) per two-second interval instead of one query per visible creature, and load-testing all game-object spawns no longer converts every row to a dict up front.

### Packet logs

Every session writes a `logs/packets_<timestamp>.log` dump by default. Records are queued from the asyncio thread and written by a background thread, which flushes once per second; shutdown (including Ctrl+C and the `run_*.py` exit path) drains the queue and flushes, so a crash still leaves the useful tail of the log. Packet logging previously called `flush()` after every packet, which the stall watchdog caught blocking the loop for up to 6.7 s on slow Windows disks.

Disable it when you do not need packet-level debugging:

```yaml
logging:
  packet_log: false
```

### Stopping the servers

```bash
python tools/stop_servers.py          # stops this project's run_*.py processes
python tools/stop_servers.py --force  # kill whatever owns the port
```

### Database initialization

The repository ships **no** `.db` files. Three SQLite databases are built
locally:

| File | Contents | Created by |
| --- | --- | --- |
| `data/auth.db` | accounts and the realm list | `tools/init_db.py` |
| `data/characters.db` | characters, inventory, spells and quest logs | `tools/init_db.py` / first worldserver start |
| `data/world.db` | static world bank (creatures, items, quests, spells, loot) | `tools/import_wotlk_db.py` |

```bash
# Create the auth/character schemas and an empty world schema
python tools/init_db.py

# Build the world bank from the pinned CMaNGOS dump
python tools/import_wotlk_db.py
```

`data/world.db` is generated, not committed: it is reproducible because
`tools/import_wotlk_db.py` downloads a specific `cmangos/wotlk-db` commit and
verifies its SHA-256 before importing.  Characters are stored separately, so
regenerating the world bank never touches player data.  A legacy combined
`data/world.db` is migrated automatically on the first split startup: any
characters, items, spells and quest logs it contains are copied into
`data/characters.db`, and the old file is left untouched.

The worldserver also creates and migrates the character, creature and spell tables when it starts, so a checkout made before a schema addition still works without re-running `init_db.py`.  The GitHub Actions test workflow caches the pinned dump and runs `python tools/import_wotlk_db.py --db data/world.db --combined`; the unit tests use that single-file layout, while a normal server still splits characters into `data/characters.db`.  Tests that need the generated world bank skip automatically when it is absent.

### Downloading terrain, VMaps and MMaps

Real line of sight and wayfinding need the client extracts, which are too large to keep in the repository.  They are cached under `data/client` (gitignored):

```bash
# Northshire/Elwynn starter area (map 0, tile 48,32 +/- 2 tiles)
python tools/fetch_client_data.py --preset starter

# Custom area or an entire map (the whole map is several hundred MB)
python tools/fetch_client_data.py --map 0 --around -8955,-132 --radius 4
python tools/fetch_client_data.py --map 0 --all-map
```

The default mirror is a public 3.3.5a extract; `--source` accepts another mirror with the same `maps/`, `vmaps/`, `mmaps/` layout.  Stop and restart the worldserver after populating a new area so tile caches start clean.  Missing tiles are safe: the server logs nothing and falls back to clear LOS/straight-line movement for that tile.

### Importing the CMaNGOS 3.3.5a world database

`data/world.db` is not shipped.  The importer downloads the pinned
[cmangos/wotlk-db](https://github.com/cmangos/wotlk-db) release itself, keeps
it under `data/cache/` (gitignored), verifies the SHA-256 and only then imports,
so every checkout generates the same world bank:

```bash
# Stop the worldserver first.  The dump is downloaded and verified once.
python tools/import_wotlk_db.py

# Optional client DBCs that are not part of the SQL dump:
python tools/import_wotlk_db.py \
    --rune-cost-dbc "C:\path\to\client\DBFilesClient\SpellRuneCost.dbc" \
    --spell-duration-dbc "C:\path\to\client\DBFilesClient\SpellDuration.dbc"

# Offline machine: download WoTLKDB_*.sql.gz yourself and pass it explicitly.
python tools/import_wotlk_db.py --dump C:\path\to\WoTLKDB_1_9_14092.sql.gz
```

The pinned commit/file/hash live at the top of `tools/import_wotlk_db.py`
(`CMANGOS_COMMIT`, `CMANGOS_DUMP_FILE`, `CMANGOS_DUMP_SHA256`); bump all of
them together when intentionally upgrading.  `--refresh-dump` re-downloads a
cached copy, and `--cache-dir` picks another cache location.

The importer reads the MySQL dump dependency-free and prints how many rows it loaded plus the number of visible spawns at every race's starting point.  `SpellDuration.dbc` fills the `spell_duration` table used by summoning (warlock pets are permanent via index 21); without it the server falls back to a small built-in duration table.

### Client setup

#### realmlist.wtf

Point the client at the authserver:

```
set realmlist 127.0.0.1
```

If the client is on another machine, set `authserver.realm_address` to the server's LAN IP.

#### Apply client settings

On a clean 3.3.5a install, friendly nameplates are off. Run this once for every client folder:

```bash
python tools/apply_client_settings.py "C:\path\to\client"
python tools/apply_client_settings.py --search   # patch every client under Desktop/Documents
```

This writes `realmlist.wtf` and the nameplate CVars into `WTF/Config.wtf`:

```
SET nameplateShowFriends "1"
SET nameplateShowEnemies "1"
```

#### ManaWoWNameplates addon

The bundled addon ensures nameplate CVars stay enabled after entering the world:

```bash
# Addon is automatically copied by apply_client_settings.py
```

### Verifying without a client

`tools/protocol_selftest.py` starts both servers in-process against temporary databases and simulates full client sessions:

```bash
python tools/protocol_selftest.py
```

Expected final line:

```
[5/5] PASS - auth -> realm list -> world auth -> character select -> world entry -> movement -> cross-account relay -> logout works
```

Smoke tests:

```bash
python tools/protocol_selftest.py  # creature AI, runes, idle movement, summons
python tools/summon_smoke.py       # pets/guardians against the real world DB
```

## Layout

```
wow335a-pyserver/
├── config/        # YAML config
├── data/          # SQLite databases
├── info/          # protocol notes/opcode dumps
├── logs/          # runtime logs
├── run_auth.py
├── run_world.py
├── run_servers.py
├── tools/
│   ├── init_db.py
│   ├── import_wotlk_db.py   # CMaNGOS world dump -> NPC tables
│   ├── wotlk_dump.py        # dependency-free MySQL dump parser
│   ├── create_test_account.py
│   ├── apply_client_settings.py  # realmlist + nameplate CVars for a client
│   ├── protocol_selftest.py
│   ├── summon_smoke.py
│   └── stop_servers.py
└── src/
    ├── auth/      # authserver + realm list
    ├── common/    # config, logging, utils
    ├── crypto/    # SRP6 + world header RC4 crypt
    ├── network/   # packet headers/logging
    ├── protocol/  # 3.3.5a opcodes
    └── world/
        ├── worldsocket/  # refactored WorldSocket mixins
        │   ├── __init__.py
        │   ├── constants.py
        │   ├── base.py
        │   ├── auth.py
        │   ├── peers.py
        │   ├── characters.py
        │   ├── world_entry.py
        │   ├── player_movement.py
        │   ├── chat.py
        │   ├── stats.py
        │   ├── auras.py
        │   ├── runes.py
        │   ├── spells.py
        │   ├── combat.py
        │   ├── creatures.py
        │   ├── creature_ai.py
        │   ├── creature_movement.py
        │   ├── summons.py
        │   ├── loot.py
        │   ├── gameobjects.py
        │   └── inventory.py
        ├── server.py
        ├── realmsocket.py  # thin facade, re-exports WorldSocket + constants
        ├── object_update.py
        ├── movement.py
        ├── creature.py
        ├── gameobject.py  # game object schema, map grid, create blocks, query
        ├── spells.py
        ├── rune_costs.py
        ├── playercreateinfo.sql  # authentic level-1 spells + action bars
        ├── update_fields.py
        ├── login_packets.py
        ├── character_db.py
        ├── addon_info.py
        └── __init__.py
```

## Protocol notes

Useful details are documented in the code:

- Client headers are 6 bytes: `uint16 size` (big-endian, includes the 4-byte opcode) + `uint32 opcode` (little-endian).
- Server headers are `uint16 size` (big-endian, payload + 2-byte opcode) + `uint16 opcode` (little-endian); packets over `0x7FFF` use a 3-byte size.
- Only world packet **headers** are RC4-encrypted; payloads stay plaintext.
- The 40-byte SRP6 session key is `SHA1Interleave(S)` and must be stored exactly as 40 bytes.
- `SMSG_AURA_UPDATE` is `packed target guid`, then `u8 slot`, and either `u32 0` for removal or `u32 spell, u8 flags, u8 caster level, u8 stacks`; unless `AFLAG_CASTER` (0x08) is set, a packed caster guid follows; with `AFLAG_DURATION` (0x20), `u32 max duration` and `u32 remaining` follow.
- `SMSG_SPELLLOGMISS` is `u32 spell, raw u64 caster, u8 unknown, u32 count`, then each miss as `raw u64 target, u8 miss type` (plus a `u8 reflect result` for reflected spells).
- `MSG_CHANNEL_START` is `packed caster, u32 spell, u32 duration`; `MSG_CHANNEL_UPDATE` is `packed caster, u32 remaining`.
- `SMSG_SPELLENERGIZELOG` is `packed victim, packed caster, u32 spell, u32 power type, u32 amount`.

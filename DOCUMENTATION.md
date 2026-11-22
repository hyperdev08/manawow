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
- Accounts, realms and characters

### Item system (initial milestone)
- `SMSG_ITEM_QUERY_SINGLE_RESPONSE` from `item_templates` SQLite table
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
- `Summon Imp` (688) is the only deliberate addition to the authentic list: it is trainer-taught at level 1 and this server has no class trainers, so warlocks would otherwise have no summon
- `SMSG_LOGIN_VERIFY_WORLD` is the first world packet, followed by `SMSG_INITIAL_SPELLS` and `SMSG_ACTION_BUTTONS`
- `CMSG_CAST_SPELL` / `CMSG_CANCEL_CAST` use imported `spell_templates`: cast time, range, power cost, GCD (`StartRecoveryTime`), school, damage/heal rolls and effect/aura enums
- Melee auto-attack, weapon damage/delay, creature aggro + chase + leash AI, creature melee, death, respawn, XP and level-up
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
- Death knight runes are real state: blood, blood, unholy, unholy, frost, frost, regenerated at 0.1 per second
- Area-of-effect spells use the real 3.3.5 `EffectImplicitTarget`/`SpellRadius.dbc` data:
  - self-centred areas (Arcane Explosion, Thunder Clap, Frost Nova, Whirlwind, Blood Boil, Consecration) select every enemy within the effect radius
  - ground-targeted areas (Flamestrike, Blizzard, Rain of Fire, Volley, Earthquake, Death and Decay) read the click destination from the cast packet and place the effect there
  - 90-degree cones (Cone of Cold, Shockwave) check the caster's facing
  - chain spells (Chain Lightning, Multi-Shot) jump through up to `EffectChainTargets` nearby enemies with the reference 30% falloff per jump
  - persistent ground effects tick from `SpellDuration.dbc` and take their damage from the periodic aura or its `PERIODIC_TRIGGER_SPELL` (e.g. Blizzard 42208, Rain of Fire 42223)
  - `MaxTargets` is honoured (Whirlwind hits four), the caster's own pet is excluded, and creature AoE (Flamestrike) damages the player and leaves a hostile ground effect
  - party/raid-only heals apply to the caster because there is no group system yet; slow/root components are not implemented

### Auras (buffs, debuffs and movement effects)
- A real aura registry for players (`combat["auras"]`) and creatures (`combat["creature_auras"]`), keyed by spell so a recast refreshes the duration and stacks up to `Spell.dbc` `StackAmount`
- Components implemented: periodic damage/heal/leech, periodic energize, stun (12), root (26), silence (27), pacify (25), movement increase/decrease (31/33), primary stats (29), health/energy (34/35), armor/resistances (22), attack power (99/124) and melee haste (138)
- Durations come from `SpellDuration.dbc` (full 3.3.5 fallback table); expired auras remove their modifiers and resend the affected fields
- Player movement: `SMSG_FORCE_RUN_SPEED_CHANGE`, `SMSG_FORCE_MOVE_ROOT` / `SMSG_FORCE_MOVE_UNROOT`; stuns additionally set `UNIT_FLAG_STUNNED`
- Creature movement: slows scale chase/idle speed, roots stop chasing and idle wandering, stuns stop the AI entirely and are mirrored with `UNIT_FLAG_STUNNED`; silence blocks casts and pacify blocks melee swings
- Player melee damage now includes the `attack_power / 14 * weapon speed` contribution, so Battle Shout and stat auras change real swings
- Periodic auras refresh instead of stacking duplicate DoTs and use their real duration (Fireball's 4 s burn instead of a flat 15 s)
- `SMSG_AURA_UPDATE` (buff/debuff icons) is not sent yet; dispels, immunities and charm/fear are also not implemented, and auras are session-scoped (they do not survive logout)

### Summoning (pets and guardians)
- `SPELL_EFFECT_SUMMON_PET` (56, e.g. Summon Imp/Voidwalker/Succubus/Felhunter) and `SPELL_EFFECT_SUMMON` (28, guardians such as Mirror Image) create a session-local creature next to the caster
- The summon is sent as a normal `TYPEID_UNIT` create block with `UNIT_FIELD_SUMMONEDBY`/`UNIT_FIELD_CREATEDBY` (owner guid, indices 14-17), `UNIT_FLAG_PLAYER_CONTROLLED` and the owner's faction template, so the client draws it as the player's pet
- Create/move/destroy blocks are relayed to other players on the same map; a player who enters later receives the existing pet's create block as well
- The pet follows its owner when farther than 4 yards, chases enemies the owner is fighting (leash 40 yards) and swings on a 2 s timer into the same `SMSG_ATTACKERSTATEUPDATE` / damage path as creature melee
- A second summon replaces the first (warlock demons dismiss the old pet when the replacement finishes); logout and disconnect remove the pet for every client
- Summon duration comes from `SpellDuration.dbc`: index 21 (`-1`, until dismissed) covers warlock pets, index 0 is treated as permanent, and the table has fallbacks for the common timed guardians until `--spell-duration-dbc` is imported
- `Summon Imp` is part of the level-1 warlock starter kit, matching the real class; Call Pet (883) has no stored-pet state and is refused instead of summoning nothing
- Summoned gameobjects (`SPELL_EFFECT_SUMMON_OBJECT_WILD` 76 / `SUMMON_OBJECT_SLOT1` 104, e.g. traps) are recognised but ignored because the server has no gameobject model
- No pet action bar (`SMSG_PET_SPELLS`), pet commands or stables yet: the pet fights automatically and cannot be told to stay/attack a specific target

### Creature spell-list AI
- Creatures use database spell lists during combat
- 1200 ms tick with `Availability` rolled once when the list is assigned
- `Probability` as a selection weight, `InitialMin/Max` and `RepeatMin/Max` in milliseconds
- Target ids below 100 use hardcoded rules; ids 100+ resolve through `creature_spell_targeting`
- Creatures with `SpellList = 0` fall back to legacy `creature_template_spells` plus `creature_cooldowns`
- `SPELL_ATTR_EX_NO_AUTOCAST_AI` (`0x00020000`) spells are skipped
- `SPELL_EFFECT_ACTIVATE_RUNE` (146) not implemented

### Idle creature movement
- Wander: random destination within `spawndist` radius, pause 3-10 s, walk speed, max 3 consecutive moves
- Waypoints: follow `creature_movement`/`creature_movement_template` node by node
- Runtime selection: spawn `movement_type` >=0 overrides template; 0=idle, 1=wander, 2=waypoint; -1 uses path if present, NPC units idle, wild creatures wander

### Loot
- `creature_loot_template` (218,066 rows) and `reference_loot_template` (44,244 rows / 896 pools) imported from CMaNGOS
- Loot is rolled exactly once when the creature dies, stored in `creature_spawns.loot_json`
- `CMSG_LOOT`, `CMSG_AUTOSTORE_LOOT_ITEM`, `CMSG_LOOT_MONEY`, `CMSG_LOOT_RELEASE`

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

### Stopping the servers

```bash
python tools/stop_servers.py          # stops this project's run_*.py processes
python tools/stop_servers.py --force  # kill whatever owns the port
```

### Database initialization

```bash
# Create/upgrade data/auth.db and data/world.db
python tools/init_db.py
```

The worldserver also creates and migrates the character, creature and spell tables when it starts, so a checkout made before a schema addition still works without re-running `init_db.py`.

### Importing the CMaNGOS 3.3.5a world database

The world dump can be downloaded from the [cmangos/wotlk-db](https://github.com/cmangos/wotlk-db) repository (`Full_DB/WoTLKDB_*.sql`).

```bash
# Stop the worldserver first; the import replaces the demo NPC population.
python tools/import_wotlk_db.py --dump C:\path\to\WoTLKDB.sql

# Optional client DBCs that are not part of the SQL dump:
python tools/import_wotlk_db.py --dump C:\path\to\WoTLKDB.sql \
    --rune-cost-dbc "C:\path\to\client\DBFilesClient\SpellRuneCost.dbc" \
    --spell-duration-dbc "C:\path\to\client\DBFilesClient\SpellDuration.dbc"
```

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
        │   └── inventory.py
        ├── server.py
        ├── realmsocket.py  # thin facade, re-exports WorldSocket + constants
        ├── object_update.py
        ├── movement.py
        ├── creature.py
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
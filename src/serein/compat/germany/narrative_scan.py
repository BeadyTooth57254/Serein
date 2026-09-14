"""Germany Narrative Scout orchestration with explicit source/store adapters."""
from __future__ import annotations
import hashlib
import json as _json_lib
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
from ...recall.germany.utils import strip_wikilinks
from .narrative_revision_scout import build_keyword_corridors, propose_new_roll_candidates
logger=logging.getLogger(__name__)

def _bool_value(value, default=False):
    if isinstance(value,bool):return value
    if value is None:return default
    return str(value).strip().lower() in ('1','true','yes','on')

def _int_between(value,default,low=1,high=10):
    try:return max(low,min(high,int(value)))
    except (TypeError,ValueError):return default

def _is_canonical_scene_bucket(bucket):
    meta=bucket.get('metadata',{})
    return meta.get('object_kind')=='scene' or meta.get('memory_value_source')=='authored_scene'

class NarrativeScout:

    def _narrative_revision_scan_settings(self, config_arg: dict | None=None) -> dict[str, Any]:
        cfg_source = config_arg if isinstance(config_arg, dict) else self.config
        roll_cfg = cfg_source.get('narrative_rolls', {})
        if not isinstance(roll_cfg, dict):
            roll_cfg = {}
        timezone_name = str(roll_cfg.get('revision_scan_timezone') or 'Asia/Shanghai').strip()
        try:
            scan_timezone = ZoneInfo(timezone_name)
        except Exception:
            scan_timezone = ZoneInfo('Asia/Shanghai')
        return {'enabled': _bool_value(roll_cfg.get('revision_scan_enabled'), True), 'hour': _int_between(roll_cfg.get('revision_scan_hour'), 4, 0, 23), 'minute': _int_between(roll_cfg.get('revision_scan_minute'), 0, 0, 59), 'timezone': scan_timezone, 'check_interval_seconds': _int_between(roll_cfg.get('revision_scan_check_interval_minutes'), 15, 1, 1440) * 60, 'new_roll_scout_enabled': _bool_value(roll_cfg.get('new_roll_scout_enabled'), True), 'new_roll_scout_base_url': str(roll_cfg.get('new_roll_scout_base_url') or '').strip().rstrip('/'), 'new_roll_scout_model': str(roll_cfg.get('new_roll_scout_model') or '').strip(), 'new_roll_scout_api_key_env': str(roll_cfg.get('new_roll_scout_api_key_env') or 'SEREIN_SCOUT_KEY').strip(), 'new_roll_scout_seed_limit': _int_between(roll_cfg.get('new_roll_scout_seed_limit'), 24, 2, 80), 'new_roll_scout_keywords_per_seed': _int_between(roll_cfg.get('new_roll_scout_keywords_per_seed'), 8, 2, 16), 'new_roll_scout_candidates_per_seed': _int_between(roll_cfg.get('new_roll_scout_candidates_per_seed'), 12, 2, 24)}

    def _narrative_timestamp(self, value: Any, scan_timezone: ZoneInfo) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=scan_timezone)
        return parsed.astimezone(timezone.utc)

    async def _narrative_material_freshness(self, narrative: dict, scan_timezone: ZoneInfo) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        automatic_linked_at = {str(link.get('event_id') or ''): str(link.get('linked_at') or '') for link in narrative.get('automatic_event_links') or [] if str(link.get('event_id') or '')}
        for event_id in list(dict.fromkeys(narrative.get('linked_event_ids') or [])):
            event = self.events.read(str(event_id), include_sources=False)
            if not event or str(event.get('status') or '') != 'active':
                continue
            event_updated_at = str(event.get('created_at') or '')
            link_updated_at = automatic_linked_at.get(str(event_id), '')
            updated_at = max((event_updated_at, link_updated_at), key=lambda value: self._narrative_timestamp(value, scan_timezone) or datetime.min.replace(tzinfo=timezone.utc))
            if self._narrative_timestamp(updated_at, scan_timezone) is None:
                continue
            sources.append({'source_type': 'event', 'source_id': str(event_id), 'updated_at': updated_at, 'title': str(event.get('title') or event_id), 'excerpt': str(event.get('body') or ''), 'source_sha256': str(event.get('fingerprint') or '')})
        for scene_id in list(dict.fromkeys(narrative.get('linked_scene_ids') or [])):
            scene = await self.scenes.get(str(scene_id))
            if not scene:
                continue
            metadata = scene.get('metadata', {}) if isinstance(scene.get('metadata'), dict) else {}
            updated_at = str(metadata.get('updated_at') or metadata.get('created') or metadata.get('created_at') or '')
            if self._narrative_timestamp(updated_at, scan_timezone) is None:
                continue
            content = str(scene.get('content') or '')
            sources.append({'source_type': 'scene', 'source_id': str(scene_id), 'updated_at': updated_at, 'title': str(metadata.get('name') or scene_id), 'excerpt': content, 'source_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
        for source_type, ids in (('diary', narrative.get('linked_diary_ids') or []), ('darkroom', narrative.get('linked_darkroom_ids') or [])):
            for source_id in list(dict.fromkeys(ids)):
                item = self.diaries.read(diary_id=int(source_id), limit=1, include_archived=True)
                if item.get('count') != 1:
                    continue
                updated_at = str(item.get('updated_at') or item.get('created_at') or item.get('date') or '')
                if self._narrative_timestamp(updated_at, scan_timezone) is None:
                    continue
                content = str(item.get('content') or '')
                sources.append({'source_type': source_type, 'source_id': str(source_id), 'updated_at': updated_at, 'title': str(item.get('title') or f'{source_type} {source_id}'), 'excerpt': content, 'source_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
        return sources

    def _narrative_material_link_index(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        event_links: dict[str, set[str]] = {}
        scene_links: dict[str, set[str]] = {}
        for roll in self.rolls._load():
            if str(roll.get('lifecycle') or 'active') != 'active':
                continue
            narrative_id = str(roll.get('narrative_id') or '').strip()
            if not narrative_id:
                continue
            for event_id in roll.get('linked_event_ids') or []:
                safe_id = str(event_id or '').strip()
                if safe_id:
                    event_links.setdefault(safe_id, set()).add(narrative_id)
            arc_key = str(roll.get('arc_key') or '').strip()
            if arc_key:
                for link in self.events.arc_event_links(arc_key):
                    safe_id = str(link.get('event_id') or '').strip()
                    if safe_id:
                        event_links.setdefault(safe_id, set()).add(narrative_id)
            for scene_id in roll.get('linked_scene_ids') or []:
                safe_id = str(scene_id or '').strip()
                if safe_id:
                    scene_links.setdefault(safe_id, set()).add(narrative_id)
        return (event_links, scene_links)

    async def _active_narrative_material_inventory(self) -> list[dict[str, Any]]:
        """Return every non-archived Event and canonical Scene for lexical one-hop search."""
        event_links, scene_links = self._narrative_material_link_index()
        materials: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.events.list(item_type='event', status='active', limit=500, offset=offset, include_sources=False)
            items = page.get('items') or []
            for event in items:
                event_id = str(event.get('item_id') or '').strip()
                if not event_id:
                    continue
                bound_ids = sorted(event_links.get(event_id, set()))
                materials.append({'source_type': 'event', 'source_id': event_id, 'date': str(event.get('local_date') or ''), 'title': str(event.get('title') or ''), 'summary': str(event.get('body') or ''), 'source_excerpt': '', 'search_text': '\n'.join((str(event.get('title') or ''), str(event.get('body') or ''))), 'updated_at': str(event.get('created_at') or ''), 'fingerprint': str(event.get('fingerprint') or ''), 'bound_narrative_ids': bound_ids, 'is_unbound': not bound_ids})
            offset += len(items)
            if not items or offset >= int(page.get('count') or 0):
                break
        for scene in await self.scenes.list_all(include_archive=False):
            if not _is_canonical_scene_bucket(scene):
                continue
            meta = scene.get('metadata', {}) if isinstance(scene.get('metadata'), dict) else {}
            if meta.get('active') is False or bool(meta.get('deprecated')):
                continue
            if str(meta.get('scene_status') or 'active').strip().lower() not in {'', 'active'}:
                continue
            scene_id = str(scene.get('id') or '').strip()
            content = strip_wikilinks(str(scene.get('content') or '')).strip()
            if not scene_id or not content:
                continue
            title = str(meta.get('name') or meta.get('title') or scene_id)
            bound_ids = sorted(scene_links.get(scene_id, set()))
            materials.append({'source_type': 'scene', 'source_id': scene_id, 'date': str(meta.get('date') or meta.get('event_date') or meta.get('created') or ''), 'title': title, 'summary': content[:1200], 'source_excerpt': content[:1800], 'search_text': '\n'.join((title, content)), 'updated_at': str(meta.get('updated_at') or meta.get('created') or ''), 'fingerprint': hashlib.sha256(content.encode('utf-8')).hexdigest(), 'bound_narrative_ids': bound_ids, 'is_unbound': not bound_ids})
        return materials

    def _narrative_seed_sort_key(self, item: dict[str, Any]) -> tuple[str, str, str]:
        return (str(item.get('updated_at') or item.get('date') or ''), str(item.get('source_type') or ''), str(item.get('source_id') or ''))

    def _hydrate_event_scout_material(self, item: dict[str, Any]) -> dict[str, Any]:
        if str(item.get('source_type') or '') != 'event':
            return dict(item)
        event_id = str(item.get('source_id') or '')
        event = self.events.read(event_id, include_sources=True)
        if not event or str(event.get('status') or '') != 'active':
            raise RuntimeError(f'narrative_scout_event_drift:{event_id}')
        excerpts = []
        for ref in event.get('source_refs') or []:
            content = str(ref.get('content') or '').strip()
            expected_hash = str(ref.get('content_sha256') or '').strip().lower()
            if not content or hashlib.sha256(content.encode('utf-8')).hexdigest() != expected_hash:
                raise RuntimeError(f'narrative_scout_event_source_drift:{event_id}')
            excerpts.append(content)
        hydrated = dict(item)
        hydrated['source_excerpt'] = '\n'.join(excerpts)[:2400]
        hydrated['fingerprint'] = str(event.get('fingerprint') or '')
        return hydrated

    def _hydrate_scout_corridors(self, corridors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cache: dict[str, dict[str, Any]] = {}

        def hydrate(item: dict[str, Any]) -> dict[str, Any]:
            key = f"{item.get('source_type')}:{item.get('source_id')}"
            if key not in cache:
                cache[key] = self._hydrate_event_scout_material(item)
            return dict(cache[key])
        hydrated = []
        for corridor in corridors:
            hydrated.append({**corridor, 'seed': hydrate(corridor['seed']), 'candidates': [hydrate(item) for item in corridor.get('candidates') or []]})
        return hydrated

    def _hydrate_scout_seeds(self, seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._hydrate_event_scout_material(item) for item in seeds]

    async def _scan_narrative_revision_inbox(self, *, include_external: bool=True, force_external: bool=False) -> dict[str, Any]:
        """Create review hints only; never invoke Narrative Writer or publish a roll."""
        settings = self._narrative_revision_scan_settings(self.config)
        scan_timezone = settings['timezone']
        stale_created: list[dict[str, Any]] = []
        stale_narrative_ids: set[str] = set()
        checked_rolls = 0
        for summary in self.rolls.revision_targets():
            narrative_id = str(summary.get('narrative_id') or '')
            narrative = await self.read_narrative(narrative_id)
            if narrative.get('status') != 'ok':
                continue
            published = self._narrative_timestamp(narrative.get('published_at'), scan_timezone)
            if published is None:
                continue
            checked_rolls += 1
            sources = await self._narrative_material_freshness(narrative, scan_timezone)
            if not sources:
                continue
            latest = max(sources, key=lambda source: self._narrative_timestamp(source.get('updated_at'), scan_timezone) or datetime.min.replace(tzinfo=timezone.utc))
            latest_time = self._narrative_timestamp(latest.get('updated_at'), scan_timezone)
            if latest_time and latest_time > published:
                stale_narrative_ids.add(narrative_id)
                stale_created.extend(self.inbox.consider_stale_roll(narrative, latest_material=latest, material_count=len(sources)))
        stale_hints_removed = self.inbox.reconcile_stale_rolls(stale_narrative_ids)
        scout_status = 'disabled'
        scout_model = str(settings['new_roll_scout_model'])
        scout_base_url = str(settings['new_roll_scout_base_url'])
        scout_api_key_env = str(settings['new_roll_scout_api_key_env'])
        candidate_created: list[dict[str, Any]] = []
        inventory = await self._active_narrative_material_inventory()
        bound_new_roll_hints_removed = self.inbox.reconcile_bound_new_roll_materials(bound_event_ids={str(item.get('source_id') or '') for item in inventory if str(item.get('source_type') or '') == 'event' and list(item.get('bound_narrative_ids') or [])}, bound_scene_ids={str(item.get('source_id') or '') for item in inventory if str(item.get('source_type') or '') == 'scene' and list(item.get('bound_narrative_ids') or [])})
        seeds = sorted((item for item in inventory if bool(item.get('is_unbound'))), key=self._narrative_seed_sort_key, reverse=True)[:int(settings['new_roll_scout_seed_limit'])]
        scout_input_fingerprint = hashlib.sha256(_json_lib.dumps([(str(item.get('source_type') or ''), str(item.get('source_id') or ''), str(item.get('updated_at') or ''), str(item.get('fingerprint') or ''), tuple(item.get('bound_narrative_ids') or [])) for item in sorted(inventory, key=lambda row: (str(row.get('source_type') or ''), str(row.get('source_id') or '')))], ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        previous_scan = self.inbox.scan_metadata()
        from ...deployment import task_model
        configured_model = task_model(self.settings.database, 'narrative_scout') if hasattr(self, 'settings') else None
        if include_external and settings['new_roll_scout_enabled']:
            if not seeds:
                scout_status = 'no_materials'
            elif scout_input_fingerprint == previous_scan.get('external_input_sha256') and (not force_external):
                scout_status = 'unchanged'
            elif not configured_model and (not scout_base_url or not scout_model or (not scout_api_key_env) or (not os.environ.get(scout_api_key_env))):
                scout_status = 'unavailable'
            else:
                client = None
                try:
                    if configured_model:
                        from ...model_runtime import TaskClient
                        client = TaskClient(self.settings.database, 'narrative_scout')
                    else:
                        from openai import AsyncOpenAI
                        client = AsyncOpenAI(api_key=os.environ[scout_api_key_env], base_url=scout_base_url, timeout=180.0, max_retries=0)
                    role_rules = self.role_rules()
                    hydrated_seeds = self._hydrate_scout_seeds(seeds)
                    hydrated_by_key = {f"{str(item.get('source_type') or '')}:{str(item.get('source_id') or '')}": item for item in hydrated_seeds}
                    search_inventory = [hydrated_by_key.get(f"{str(item.get('source_type') or '')}:{str(item.get('source_id') or '')}", item) for item in inventory]
                    corridors = build_keyword_corridors(search_inventory, list(hydrated_by_key), max_keywords=int(settings['new_roll_scout_keywords_per_seed']), max_candidates_per_seed=int(settings['new_roll_scout_candidates_per_seed']))
                    hydrated_corridors = self._hydrate_scout_corridors(corridors)
                    existing_candidates = self.inbox.candidate_contexts(hydrated_corridors)
                    candidates = await propose_new_roll_candidates(client=client, model=scout_model, corridors=hydrated_corridors, role_rules=role_rules, completion_options={'max_tokens': 2600, 'temperature': 0.0}, existing_candidates=existing_candidates)
                    candidate_created = self.inbox.consider_new_roll_candidates(candidates, model=scout_model)
                    scout_status = 'ok' if corridors else 'no_keyword_matches'
                except Exception as exc:
                    scout_status = 'error'
                    logger.warning('New Narrative Roll scout failed / 新叙事卷候选扫描失败: %s', exc)
                finally:
                    if client is not None:
                        await client.close()
        recorded_fingerprint = scout_input_fingerprint if scout_status in {'ok', 'unchanged', 'no_materials', 'no_keyword_matches'} else str(previous_scan.get('external_input_sha256') or '')
        result = {'status': 'ok', 'checked_rolls': checked_rolls, 'stale_roll_hints_created': len(stale_created), 'stale_roll_hints_removed': len(stale_hints_removed), 'bound_new_roll_hints_removed': len(bound_new_roll_hints_removed), 'active_materials_searched': len(inventory), 'unbound_material_seeds_checked': len(seeds), 'unbound_events_checked': sum((1 for item in seeds if str(item.get('source_type') or '') == 'event')), 'unbound_scenes_checked': sum((1 for item in seeds if str(item.get('source_type') or '') == 'scene')), 'new_roll_hints_created': sum(item.get('change') == 'created' for item in candidate_created), 'new_roll_hints_accumulated': sum(item.get('change') == 'accumulated' for item in candidate_created), 'external_scout_status': scout_status, 'external_model': scout_model if scout_status not in {'disabled', 'unavailable'} else '', 'external_base_url': scout_base_url if scout_status not in {'disabled', 'unavailable'} else '', 'external_search_mode': 'host_literal_keywords_then_active_exact_search_then_terra_review', 'external_input_sha256': recorded_fingerprint, 'writes_performed': [{'type': 'narrative_revision_hint', 'proposal_id': item.get('proposal_id'), 'proposal_kind': item.get('proposal_kind')} for item in [*stale_created, *candidate_created]] + [{'type': 'narrative_revision_hint_removed', 'proposal_id': proposal_id, 'proposal_kind': 'existing_roll_update'} for proposal_id in stale_hints_removed], 'narrative_writes_performed': []}
        self.inbox.record_scan(result)
        return result

"""Durable Scene work and existing Germany schedules, independent of chat windows."""

import asyncio
import logging
import time

from ..core.store import Store
from .background import SceneReader, DreamMaterialReader, EmbeddingCandidates, germany_config
from .germany.scene_linker import SceneLinker
from .scout import Scout
from .dreams import Dreams
from .dream_vectors import DreamVectors

logger = logging.getLogger(__name__)


async def update_scene_jobs(settings, linker):
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute('SELECT * FROM scene_jobs ORDER BY sequence LIMIT 100').fetchall()
    if not rows:
        return {'status': 'current'}
    scenes = SceneReader(settings.database)
    for key in dict.fromkeys(row['scene_id'] for row in rows):
        with Store(settings.database, read_only=True) as store:
            doc = store.read(key)
        if doc is None or doc['lifecycle'] != 'active':
            linker.deactivate_scene_edges(key, reason='scene_inactive',
                lifecycle_status='archived' if doc and doc['lifecycle']=='archived' else 'cancelled')
        else:
            result = await linker.handle_scene_content_changed(key, scenes)
            if result.get('normal_relink_required') and linker.enabled and linker.auto_enabled:
                result = await linker.link_scene(key, scenes, EmbeddingCandidates(settings))
                if result.get('status') in ('failed', 'unavailable'):
                    raise RuntimeError('Scene relation model unavailable; keeping pending work')
        with Store(settings.database) as store:
            store.conn.execute('DELETE FROM scene_jobs WHERE scene_id=? AND sequence<=?', (key, rows[-1]['sequence']))
    return {'status': 'updated'}


class BackgroundJobs:
    def __init__(self, settings, *, features=None):
        self.settings = settings
        features = {'relations','narrative_revision','dreams'} if features is None else features
        from ..deployment import task_model
        from ..model_runtime import TaskClient
        clients={'selected':TaskClient(settings.database,'relations')} if task_model(settings.database,'relations') else {}
        self.linker = SceneLinker(germany_config(settings),clients=clients) if 'relations' in features else None
        if self.linker:
            self.linker.proposal_store(create=True)
        self.scout = Scout(settings) if {'narrative_revision','narrative_scout'} & features else None
        self.dreams = Dreams(settings) if 'dreams' in features else None

    async def run(self):
        next_scan = time.monotonic() + 45
        next_dream = time.monotonic() + 30
        try:
            while True:
                try:
                    if self.linker:
                        await update_scene_jobs(self.settings, self.linker)
                except Exception:
                    logger.exception('Scene background work failed; keeping pending work')
                if self.scout and time.monotonic() >= next_scan:
                    try:
                        await self.scout.run_due()
                    except Exception:
                        logger.exception('Narrative scan failed')
                    next_scan = time.monotonic() + self.scout._narrative_revision_scan_settings()['check_interval_seconds']
                if self.dreams and time.monotonic() >= next_dream:
                    try:
                        await self.dreams.run_due(DreamMaterialReader(self.settings.database),DreamVectors(self.settings))
                    except Exception:
                        logger.exception('Dream generation failed')
                    next_dream=time.monotonic()+self.dreams.check_interval_minutes*60
                await asyncio.sleep(5)
        finally:
            if self.dreams and self.dreams.client:
                await self.dreams.client.close()
            for provider in self.linker.providers if self.linker else []:
                if provider.get('client'):
                    await provider['client'].close()

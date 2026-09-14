"""Start only optional tasks explicitly selected on the instance settings page."""
import asyncio
import logging
from .deployment import read_settings, configured_models
from .core.store import encode


async def run(application):
    from .compat.jobs import BackgroundJobs
    from .extensions.pipeline import scheduled_advance, ROLES
    async def pipeline():
        while True:
            try:
                await scheduled_advance(settings.database)
            except Exception:
                logging.getLogger(__name__).exception('Event pipeline failed; originals remain pending')
            await asyncio.sleep(30)
    running={}
    settings=application.settings
    try:
        while True:
            config=read_settings(settings.database)
            for feature in ('relations','dreams','narrative_scout','event_pipeline','operit_tagging'):
                if feature in application.enabled_extensions:continue
                selected=config['assignments'].get(feature)
                model=next((item for item in configured_models(config) if item['id']==selected),None)
                signature=encode(model) if model else ''
                if feature=='event_pipeline':
                    selected_models=[item for item in configured_models(config) if item['id'] in
                                     [config['assignments'].get(role) for role in ROLES]]
                    signature=encode(selected_models) if selected_models else ''
                previous=running.get(feature)
                if previous and (previous[0]!=signature or previous[1].done()):
                    previous[1].cancel();await asyncio.gather(previous[1],return_exceptions=True)
                    del running[feature]
                if signature and feature not in running:
                    try:
                        if feature=='event_pipeline':
                            coroutine=pipeline()
                        elif feature=='operit_tagging':
                            from .import_tagging import run as tag_imports
                            coroutine=tag_imports(settings)
                        else:
                            coroutine=BackgroundJobs(settings,features={feature}).run()
                        running[feature]=(signature,asyncio.create_task(coroutine,name='configured:'+feature))
                    except Exception:
                        logging.getLogger(__name__).exception('Configured task could not start: %s',feature)
            await asyncio.sleep(5)
    finally:
        for _,task in running.values():task.cancel()
        await asyncio.gather(*(task for _,task in running.values()),return_exceptions=True)

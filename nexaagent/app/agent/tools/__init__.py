from langchain_core.tools import StructuredTool

from app.agent.tools.knowledge_base import search_knowledge_base
from app.agent.tools.long_term_memory import search_long_term_memory
from app.agent.tools.web_request import http_get
from app.agent.tools.create_task import create_task
from app.agent.tools.manage_tasks import list_tasks, update_task, delete_task
from app.agent.tools.call_webhook import call_webhook
from app.agent.tools.calendar import (
    create_calendar_event,
    delete_calendar_event,
    list_calendar_events,
    update_calendar_event,
)
from app.agent.tools.gmail import send_email
from app.agent.tools.drive import list_drive_files, ingest_drive_file


def get_tools() -> list[StructuredTool]:
    return [
        search_knowledge_base,
        search_long_term_memory,
        http_get,
        create_task,
        list_tasks,
        update_task,
        delete_task,
        call_webhook,
        list_calendar_events,
        create_calendar_event,
        update_calendar_event,
        delete_calendar_event,
        send_email,
        list_drive_files,
        ingest_drive_file,
    ]
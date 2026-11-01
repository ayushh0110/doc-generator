from functools import lru_cache
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings
from app.graph import nodes
from app.graph.state import TurnState


def build_graph(checkpointer: Any = None) -> Any:
    graph = StateGraph(TurnState)
    graph.add_node("supervisor", nodes.supervisor)
    graph.add_node("template_analyst", nodes.template_analyst)
    graph.add_node("retriever", nodes.retriever)
    graph.add_node("web_researcher", nodes.web_researcher)
    graph.add_node("writer", nodes.writer)
    graph.add_node("builder", nodes.builder)
    graph.add_node("validator", nodes.validator)
    graph.add_node("gave_up", nodes.gave_up)
    graph.add_node("publisher", nodes.publisher)
    graph.add_node("responder", nodes.responder)

    research_targets = ["retriever", "web_researcher", "writer", "responder"]
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", nodes.after_supervisor, ["template_analyst", *research_targets])
    graph.add_conditional_edges("template_analyst", nodes.after_analyst, research_targets)
    graph.add_conditional_edges("retriever", nodes.after_retriever, ["web_researcher", "writer", "responder"])
    graph.add_conditional_edges("web_researcher", nodes.after_web, ["writer", "responder"])
    graph.add_conditional_edges("writer", nodes.after_writer, ["builder", "writer", "gave_up", "responder"])
    graph.add_conditional_edges("builder", nodes.after_builder, ["validator", "writer", "gave_up"])
    graph.add_conditional_edges("validator", nodes.after_validator, ["publisher", "writer", "gave_up"])
    graph.add_edge("gave_up", "responder")
    graph.add_edge("publisher", "responder")
    graph.add_edge("responder", END)
    return graph.compile(checkpointer=checkpointer)


@lru_cache
def get_graph() -> Any:
    pool = ConnectionPool(
        get_settings().sync_checkpoint_url,
        max_size=8,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    )
    checkpointer = PostgresSaver(pool)
    checkpointer.setup()
    return build_graph(checkpointer)

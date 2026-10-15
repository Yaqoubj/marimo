# Copyright 2026 Marimo. All rights reserved.
"""Translate between host records and the editor's document model.

The host owns protocol edits and etags. Sessions still own the editor's
transaction history, so the bridge mirrors each session transaction into
the host document.

While a session is attached it also owns the app config, and the host
document only mirrors it. Host edits change cells and their order.

TODO: An editor app config change produces no document transaction, so
the host does not learn of it until the next cell transaction, and
`app_config` and `app_config_etag` on the host stream lag until then.
Nothing writes app config from the host side, so no data is at risk.
Resolve once the frontend consumes the host protocol, when a config
change becomes a change on the core document like any other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from marimo._ast.cell import CellConfig
from marimo._core import protocol
from marimo._core.document import CellSource
from marimo._messaging.notebook import (
    CreateCell,
    DeleteCell,
    DocumentChange,
    ReorderCells,
    SetCode,
    SetConfig,
    SetName,
    Transaction,
)
from marimo._messaging.notification import (
    NotebookDocumentTransactionNotification,
)
from marimo._runtime.commands import DeleteCellCommand, UpdateCellConfigCommand
from marimo._types.ids import CellId_t

if TYPE_CHECKING:
    from marimo._core.document import Document
    from marimo._session.notebook import AppFileManager
    from marimo._session.session import Session


def from_session(session: Session) -> tuple[list[CellSource], dict[str, Any]]:
    cells = [
        CellSource(cell.id, cell.code, cell.name, _config(cell.config))
        for cell in session.document.cells
    ]
    return cells, session.app_file_manager.app.config.asdict_difference()


def from_file_manager(
    manager: AppFileManager,
) -> tuple[list[CellSource], dict[str, Any]]:
    cells = [
        CellSource(data.cell_id, data.code, data.name, _config(data.config))
        for data in manager.app.cell_manager.cell_data()
    ]
    return cells, manager.app.config.asdict_difference()


def _config(config: CellConfig) -> protocol.CellConfig:
    return protocol.CellConfig(
        column=config.column,
        disabled=config.disabled,
        hide_code=config.hide_code,
        expand_output=config.expand_output,
    )


def _cell_config(config: protocol.CellConfig) -> CellConfig:
    return CellConfig(
        column=config.column,
        disabled=config.disabled,
        hide_code=config.hide_code,
        expand_output=config.expand_output,
    )


def apply_to_session(session: Session, document: Document) -> None:
    """Apply the document's cells and order to the session as one transaction.

    The session tells its consumers, so the frontend sees the edit and
    the host backend mirrors it back as a no-op. The kernel is told of
    deleted cells and changed configs, as the editor tells it, so a run
    does not evaluate a cell the document no longer has or has disabled.
    New code reaches the kernel with the run that carries it. The app
    config is the session's own and is left alone.
    """
    current = session.document
    changes: list[DocumentChange] = []
    configs: dict[CellId_t, dict[str, Any]] = {}
    previous: CellId_t | None = None
    for cell in document:
        cell_id = CellId_t(cell.id)
        existing = current.get(cell_id)
        config = _cell_config(cell.config)
        if existing is None:
            changes.append(
                CreateCell(
                    cell_id=cell_id,
                    code=cell.code,
                    name=cell.name,
                    config=config,
                    after=previous,
                )
            )
            if config != CellConfig():
                configs[cell_id] = config.asdict()
        else:
            if existing.code != cell.code:
                changes.append(SetCode(cell_id=cell_id, code=cell.code))
            if existing.name != cell.name:
                changes.append(SetName(cell_id=cell_id, name=cell.name))
            if _config(existing.config) != cell.config:
                changes.append(
                    SetConfig(
                        cell_id=cell_id,
                        column=cell.config.column,
                        disabled=cell.config.disabled,
                        hide_code=cell.config.hide_code,
                        expand_output=cell.config.expand_output,
                    )
                )
                configs[cell_id] = config.asdict()
        previous = cell_id
    wanted = {CellId_t(cell.id) for cell in document}
    deleted = [c for c in current.cell_ids if c not in wanted]
    changes.extend(DeleteCell(cell_id=cell_id) for cell_id in deleted)
    order = [CellId_t(c) for c in document.order]
    if [c for c in current.cell_ids if c in wanted] != order:
        changes.append(ReorderCells(cell_ids=tuple(order)))
    if not changes:
        return
    applied = current.apply(Transaction(changes=tuple(changes), source="host"))
    session.notify(
        NotebookDocumentTransactionNotification(transaction=applied),
        from_consumer_id=None,
    )
    for cell_id in deleted:
        session.put_control_request(
            DeleteCellCommand(cell_id=cell_id), from_consumer_id=None
        )
    if configs:
        session.put_control_request(
            UpdateCellConfigCommand(configs=configs), from_consumer_id=None
        )

"""send audit sequence

Order the audit trail by a counter instead of a clock.

`send_audit` rows are written milliseconds apart — `attempted` before dispatch
and `sent` / `refused` / `unknown` after it — and `datetime.now()` advances in
~15.6 ms steps on Windows, so both rows landed on the same `attempted_at` and
the tie-break fell to a random hex id. The audit came out in a random order on
every send.

That matters more here than anywhere else in the schema: "attempted then sent"
and "attempted then unknown" are the same two rows and opposite conclusions, and
the person reading them is trying to work out whether an application actually
went out.

Revision ID: 90d9ae5ab373
Revises: 106cbd4a5ae9
Create Date: 2026-09-26 04:01:47.260482
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "90d9ae5ab373"
down_revision: Union[str, None] = "106cbd4a5ae9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `server_default="0"` is the adjustment alembic asked for: a NOT NULL
    # column cannot be added to a table that already has rows without one, and
    # this table is append-only, so any deployment that has ever sent anything
    # has rows. Existing rows get 0 and stay in `attempted_at` order, which is
    # the best that can be said about them retrospectively — the information to
    # order them properly was never recorded.
    with op.batch_alter_table("send_audit", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("send_audit", schema=None) as batch_op:
        batch_op.drop_column("seq")

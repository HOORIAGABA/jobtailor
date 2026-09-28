"""send audit seq unique

One row per (message, seq). The counter is computed as `count + 1` before the
insert, and two requests racing to send the same message would both compute
the same value. The send path now claims the run with a conditional UPDATE so
only one of them gets this far — this constraint is the second lock on the
same door: if the claim is ever weakened, the audit trail refuses to record
two "3rd" events rather than silently losing its order.

Rows written before `seq` existed all carry the server default 0 and would
collide, so they are renumbered first — per message, in `attempted_at` order
with `id` as the tie-break, which is the best available reconstruction of an
order that was never recorded.

Revision ID: b7c1e2d3f4a5
Revises: 90d9ae5ab373
Create Date: 2026-09-27 10:30:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b7c1e2d3f4a5"
down_revision: Union[str, None] = "90d9ae5ab373"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Window functions: SQLite >= 3.25 (2018) and every Postgres.
    op.execute(
        "UPDATE send_audit SET seq = numbered.n FROM ("
        "  SELECT id, ROW_NUMBER() OVER ("
        "    PARTITION BY message_id ORDER BY attempted_at, id) AS n"
        "  FROM send_audit"
        ") AS numbered WHERE send_audit.id = numbered.id"
    )
    with op.batch_alter_table("send_audit", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_send_audit_seq",
                                          ["message_id", "seq"])


def downgrade() -> None:
    with op.batch_alter_table("send_audit", schema=None) as batch_op:
        batch_op.drop_constraint("uq_send_audit_seq", type_="unique")

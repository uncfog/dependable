"""Sending: the one path from a resolved audience to rows in the delivery log.

Two steps, deliberately separate:

``record`` writes the ``Message`` and one ``MessageRecipient`` per parent, all
pending, inside a transaction. Nothing has been sent at this point, and that is
the safe state to be interrupted in -- a crashed request leaves a batch that is
visibly queued, not a batch that may or may not have gone out.

``deliver`` walks those rows and hands each to the provider. It never raises
for a delivery failure: one wrong number must not stop the other thirty-one
parents from hearing about their fees. Failures land on the row, the batch
status rolls up, and the bursar sees exactly which two of thirty-two bounced.

Doing the sending inline rather than on a queue is a considered pilot decision:
a branch's whole parent body is a few hundred rows, the console provider has no
latency to speak of, and a task queue is a piece of infrastructure the school
would have to run. When a real gateway makes the loop slow, ``deliver`` is what
moves onto a worker -- it already takes a saved batch and touches nothing else.

Every batch is sent under one school's own Sender ID, resolved once by
:mod:`apps.messaging.identity` before anything is written. Resolving it first
is what makes "a school with no approved Sender ID sends nothing" true rather
than aspirational: there is no path from here to a provider that does not go
through that resolution, and a school that fails it never gets a Message row at
all -- so the log never shows a batch that was never really sendable.

A WhatsApp batch is resolved against the school's WhatsApp approval rather than
its Sender ID, and goes out as an approved :class:`~.models.WhatsAppTemplate`
filled in per parent -- WhatsApp does not carry free text the school starts.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.students.validators import normalise_phone

from . import identity as identity_module
from .audiences import Audience
from .models import Message, MessageRecipient
from .providers import (
    Channel,
    DeliveryStatus,
    MessagePurpose,
    MessagingProvider,
    SenderIdentity,
    SendResult,
    get_provider,
)


@transaction.atomic
def record(
    *,
    body: str,
    channel: str,
    audience: Audience,
    branch,
    sender=None,
    provider_key: str = "",
    identity: SenderIdentity | None = None,
    purpose: str = MessagePurpose.TRANSACTIONAL,
    template=None,
) -> Message:
    """Write the batch as queued. Sends nothing.

    ``identity`` is resolved by the caller rather than here so that a school
    with no approved Sender ID is refused *before* any row is written -- see
    :func:`send`.
    """
    # Belt and braces over the tenant-scoped form that offered the template:
    # one school's batch must never go out as another school's template.
    if template is not None and template.school_id != branch.school_id:
        raise ValueError("That WhatsApp template belongs to a different school.")

    message = Message(
        template=template,
        branch=branch,
        school_id=branch.school_id,
        body=body,
        channel=channel,
        purpose=purpose,
        audience=audience.description,
        audience_type=audience.type,
        sender=sender,
        provider=provider_key,
        # Snapshotted, like the recipients' phone numbers: this is the name the
        # parents on this batch actually saw.
        sent_as=identity.sender_id if identity else "",
    )
    message.save()

    MessageRecipient.objects.bulk_create(
        [
            MessageRecipient(
                school_id=branch.school_id,
                branch=branch,
                message=message,
                student=student,
                # Normalised here rather than trusted from the row: a number
                # imported from a spreadsheet may still be carrying spaces.
                # The primary number only. ``parent_phone_alt`` is kept on file
                # but deliberately not texted -- see Student.parent_phone_alt.
                phone=normalise_phone(student.parent_phone),
                parent_name=student.parent_name,
                status=DeliveryStatus.PENDING,
            )
            for student in audience.students
            # Belt and braces over the tenant-scoped queryset that produced
            # them: a student from another campus never gets a row, whatever
            # the caller passed in.
            if student.branch_id == branch.pk
        ]
    )
    return message


def deliver(message: Message, provider: MessagingProvider | None = None) -> Message:
    """Push every pending recipient of ``message`` through the provider.

    Idempotent by design: rows that already have a terminal status are skipped,
    so re-running a half-finished batch finishes it rather than texting the
    parents who already heard.

    That is only true if each result is on record before the next send, so every
    row is written as it goes. gunicorn kills a worker that outlives its timeout,
    and whatever was held in memory dies with it: writing the batch at the end
    left parents who had been texted looking as if they had not. A kill between
    the gateway accepting a message and this write can still repeat that one
    message, and only that one.

    Called without a provider -- finishing a batch that was recorded earlier --
    it rebuilds one from the school's identity, so a resumed send goes out
    under the same name the batch was stamped with.
    """
    provider = provider or get_provider(identity=identity_for(message))
    pending = list(
        message.recipients.filter(status=DeliveryStatus.PENDING)
        # A template names the student; one join rather than one per parent.
        .select_related("student")
    )
    # Once per batch, and only when a template might ask for it.
    school_name = (
        message.school.name if message.channel == Channel.WHATSAPP else ""
    )

    try:
        for recipient in pending:
            result = _send_one(provider, recipient, message, school_name)
            MessageRecipient.all_objects.filter(pk=recipient.pk).update(
                status=result.status,
                provider_reference=result.reference[:120],
                error=result.error,
                sent_at=timezone.now() if result.ok else recipient.sent_at,
            )
    finally:
        # In ``finally`` so a batch that is cut off still reads as what it is on
        # the log, rather than as queued with nothing sent.
        message.refresh_status()
    return message


def _send_one(
    provider: MessagingProvider,
    recipient: MessageRecipient,
    message: Message,
    school_name: str = "",
) -> SendResult:
    """One send, with every foreseeable failure turned into a logged row.

    The bare ``except`` is intentional and is the only one in the codebase: a
    provider is third-party code over a network, and whatever it throws must
    become a red row in the delivery log rather than a 500 that loses the other
    recipients' results along with it.
    """
    if not provider.supports(message.channel):
        return SendResult.failure(
            f"{provider.label} cannot send over "
            f"{Channel(message.channel).label}."
        )

    # Read off the batch, not passed in: a send resumed days later must be
    # routed exactly as the first half of it was.
    options = {"purpose": message.purpose}
    if message.channel == Channel.WHATSAPP:
        template = message.template
        # Checked per batch at delivery, not only at compose: a template Meta
        # paused between the first half of a batch and the second must stop the
        # second half rather than have WhatsApp refuse it parent by parent.
        if template is None or not template.is_usable:
            return SendResult.failure(
                "WhatsApp only carries approved templates, and this batch's "
                "template is not approved, so it was not sent."
            )
        options["template"] = template.message_for(
            parent_name=recipient.parent_name,
            student_name=recipient.student.full_name if recipient.student else "",
            school_name=school_name,
            message=message.body,
        )

    try:
        return provider.send(
            recipient.phone, message.body, message.channel, **options
        )
    except Exception as exc:  # noqa: BLE001 -- see the docstring above
        return SendResult.failure(f"{type(exc).__name__}: {exc}")


def identity_for(message: Message) -> SenderIdentity:
    """The identity a saved batch belongs to.

    Re-resolved from the school's config rather than rebuilt from the stored
    ``sender_id``, so a batch resumed after an approval was withdrawn is
    refused rather than finished under a name that is no longer registered.
    """
    return identity_module.resolve(
        message.school_id, message.branch, channel=message.channel
    )


def send(
    *,
    body: str,
    channel: str,
    audience: Audience,
    branch,
    sender=None,
    provider: MessagingProvider | None = None,
    purpose: str = MessagePurpose.TRANSACTIONAL,
    template=None,
) -> Message:
    """Record a batch and deliver it. What the compose screen calls.

    Resolves the school's identity for ``channel`` first, and raises
    :class:`~apps.messaging.identity.SenderIdentityUnavailable` before writing
    anything if there is not an approved one. The compose screen catches that
    and shows the reason; nothing reaches a gateway under a borrowed or blank
    name, and nothing reaches WhatsApp before Meta has approved the school.

    ``template`` is the approved ``WhatsAppTemplate`` a WhatsApp batch goes out
    as; SMS batches have none.
    """
    identity = identity_module.resolve_for_branch(branch, channel=channel)
    provider = provider or get_provider(identity=identity)
    message = record(
        body=body,
        channel=channel,
        audience=audience,
        branch=branch,
        sender=sender,
        provider_key=provider.key,
        identity=identity,
        purpose=purpose,
        template=template,
    )
    return deliver(message, provider)

from django.contrib.auth import get_user_model
from django.utils.translation import ugettext as _

from misago.core.mail import build_mail, send_messages

from .events import record_event
from .models import ThreadParticipant


UserModel = get_user_model()


def has_participants(thread):
    return thread.threadparticipant_set.exists()


def make_participants_aware(user, target):
    if hasattr(target, '__iter__'):
        make_threads_participants_aware(user, target)
    else:
        make_thread_participants_aware(user, target)


def make_threads_participants_aware(user, threads):
    threads_dict = {}
    for thread in threads:
        thread.participant = None
        threads_dict[thread.pk] = thread

    participants_qs = ThreadParticipant.objects.filter(
        user=user,
        thread_id__in=list(threads_dict.keys()),
    )

    for participant in participants_qs:
        participant.user = user
        threads_dict[participant.thread_id].participant = participant


def make_thread_participants_aware(user, thread):
    thread.participants_list = []
    thread.participant = None

    participants_qs = ThreadParticipant.objects.filter(thread=thread)
    participants_qs = participants_qs.select_related('user')
    for participant in participants_qs.order_by('-is_owner', 'user__slug'):
        participant.thread = thread
        thread.participants_list.append(participant)
        if participant.user == user:
            thread.participant = participant
    return thread.participants_list


def set_users_unread_private_threads_sync(users=None, participants=None, exclude_user=None):
    users_ids = []
    if users:
        users_ids += [u.pk for u in users]
    if participants:
        users_ids += [p.user_id for p in participants]
    if exclude_user:
        users_ids = [u for u in users_ids if u != exclude_user.pk]

    if not users_ids:
        return

    UserModel.objects.filter(id__in=set(users_ids)).update(sync_unread_private_threads=True)


def set_owner(thread, user):
    ThreadParticipant.objects.set_owner(thread, user)


def change_owner(request, thread, user):
    ThreadParticipant.objects.set_owner(thread, user)
    set_users_unread_private_threads_sync(
        participants=thread.participants_list,
        exclude_user=request.user,
    )

    if thread.participant and thread.participant.is_owner:
        record_event(
            request,
            thread,
            'changed_owner',
            {
                'user': {
                    'username': user.username,
                    'url': user.get_absolute_url(),
                },
            },
        )
    else:
        record_event(request, thread, 'tookover')


def add_participant(request, thread, user):
    """adds single participant to thread, registers this on the event"""
    add_participants(request, thread, [user])

    if request.user == user:
        record_event(request, thread, 'entered_thread')
    else:
        record_event(
            request,
            thread,
            'added_participant',
            {
                'user': {
                    'username': user.username,
                    'url': user.get_absolute_url(),
                },
            },
        )


def add_participants(request, thread, users):
    """
    Add multiple participants to thread, set "recound private threads" flag on them
    notify them about being added to thread.
    """
    ThreadParticipant.objects.add_participants(thread, users)

    try:
        thread_participants = thread.participants_list
    except AttributeError:
        thread_participants = []

    set_users_unread_private_threads_sync(
        users=users,
        participants=thread_participants,
        exclude_user=request.user,
    )

    emails = []
    for user in users:
        if user != request.user:
            emails.append(build_noticiation_email(request, thread, user))
    if emails:
        send_messages(emails)


def build_noticiation_email(request, thread, user):
    subject = _('%(user)s has invited you to participate in private thread "%(thread)s"')
    subject_formats = {
        'thread': thread.title,
        'user': request.user.username,
    }

    return build_mail(
        request, user, subject % subject_formats, 'misago/emails/privatethread/added', {
            'thread': thread,
        }
    )


def remove_participant(request, thread, user):
    """remove thread participant, set "recound private threads" flag on user"""
    removed_owner = False
    remaining_participants = []

    for participant in thread.participants_list:
        if participant.user == user:
            removed_owner = participant.is_owner
        else:
            remaining_participants.append(participant.user)

    set_users_unread_private_threads_sync(participants=thread.participants_list)

    if not remaining_participants:
        thread.delete()
    else:
        thread.threadparticipant_set.filter(user=user).delete()
        thread.subscription_set.filter(user=user).delete()

        if removed_owner:
            thread.is_closed = True  # flag thread to close

            if request.user == user:
                event_type = 'owner_left'
            else:
                event_type = 'removed_owner'
        else:
            if request.user == user:
                event_type = 'participant_left'
            else:
                event_type = 'removed_participant'

        record_event(
            request,
            thread,
            event_type,
            {
                'user': {
                    'username': user.username,
                    'url': user.get_absolute_url(),
                },
            },
        )

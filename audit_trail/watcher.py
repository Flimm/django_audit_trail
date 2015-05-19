# coding=utf-8
from django.conf import settings
from django.db.models import signals, ForeignKey
from django.dispatch import receiver
from .models import AuditTrail
from .signals import audit_trail_app_ready


class AuditTrailWatcher(object):

    u"""
    Watcher class that tracks post_save and post_delete signals and generates AuditTrail records.

    Attributs:
        tracked_models (set): set of already tracked models. Used to avoid duplicate signals handlers.

    """

    tracked_models = set()

    def __init__(self, fields=None, track_related=None, notify_related=None,
                 track_only_with_related=False, excluded_fields=None):
        u"""
        Constructor

        :param fields: list fields that should be tracked. If None — all fields will be tracked.
        :param track_related: list of tracked relations. F.e. ['comment_set']
        :param notify_related: list of fields to be notified as parent. Internal use only
        :param track_only_with_related: boolean state should be AuditTrail object created or not if there is no parent
               object. F.e. if we track Post's comment_set and we don't need to track comments separately.
        :return:
        """
        self.model_class = None
        self.fields = fields
        self.notify_related = notify_related
        self.track_related = track_related
        self.track_only_with_related = track_only_with_related
        self.excluded_fields = ['id']
        if excluded_fields:
            self.excluded_fields += excluded_fields

    def contribute_to_class(self, cls, name=None):
        if cls in self.__class__.tracked_models:
            return False

        self.model_class = cls
        self.__class__.tracked_models.add(cls)
        setattr(cls, 'audit', self)
        return True

    def init_signals(self):
        signals.post_save.connect(self.on_post_save_create, sender=self.model_class, weak=False)
        signals.post_init.connect(self.on_post_init, sender=self.model_class, weak=False)
        signals.post_save.connect(self.on_post_save_update, sender=self.model_class, weak=False)
        signals.pre_delete.connect(self.on_pre_delete, sender=self.model_class, weak=False)
        signals.post_delete.connect(self.on_post_delete, sender=self.model_class, weak=False)

        self.init_related_signals()

    def init_related_signals(self):
        if not self.track_related:
            return

        for attr_name in self.track_related:
            attribute = getattr(self.model_class, attr_name)
            if hasattr(attribute, 'related'):  # related object is queryset
                related = attribute.related
                related_model = related.related_model
                related_field_name = related.field.name
            else:  # related object is FK
                related_model = attribute.field.related_field.model
                related_field_name = attribute.field.related_query_name()

                # related_query_name() returns related_name if it was set
                # but if it's not returns autogenerated related name without '_set' postfix!
                # F.e. instead of 'post_set' it'll return 'post' so we have to handle it manually
                if not hasattr(related_model, related_field_name):
                    related_field_name += '_set'
            if not hasattr(related_model, 'audit'):
                related_watcher = AuditTrailWatcher(track_only_with_related=True)
                related_watcher.contribute_to_class(related_model)
                related_watcher.init_signals()

            related_model.audit.notify_related = related_model.audit.notify_related or []
            related_model.audit.notify_related += [related_field_name]

    def serialize_object(self, instance):
        """ Returns stringified values for tracked fields. """
        data = {}
        for field in instance._meta.fields:
            # Skip untracked fields
            not_tracked_field = (self.fields is not None and field.name not in self.fields)
            if not_tracked_field or field.name in self.excluded_fields:
                continue
            data[field.name] = field.value_from_object(instance)
        return data

    def get_changes(self, old_values, new_values):
        """ Returns list of changed fields. """
        diff = {}
        old_values = old_values or {}
        new_values = new_values or {}
        fields = self.fields or [field_name.name for field_name in self.model_class._meta.fields]

        for field_name in fields:
            old_value = old_values.get(field_name, None)
            new_value = new_values.get(field_name, None)

            field = self.model_class._meta.get_field(field_name)
            if isinstance(field, ForeignKey):
                old_value = self.get_fk_value(field_name, old_value)
                new_value = self.get_fk_value(field_name, new_value)

            if field.choices:
                old_value = self.get_choice_value(field_name, old_value)
                new_value = self.get_choice_value(field_name, new_value)

            if old_value != new_value:
                diff[field_name] = {
                    'old_value': old_value,
                    'new_value': new_value
                }
        return diff

    def get_fk_value(self, field_name, value):
        if value is None:
            return None

        value = int(value)

        instance = self.model_class(**{'%s_id' % field_name: value})
        string = unicode(getattr(instance, field_name))
        return '[#%d] %s' % (value, string)

    def get_choice_value(self, field_name, value):
        if value is None:
            return None
        instance = self.model_class(**{field_name: value})
        return u'[#%s] %s' % (
            unicode(value),
            unicode(getattr(instance, 'get_%s_display' % field_name)())
        )

    def on_post_init(self, instance, sender, **kwargs):
        """Stores original field values."""
        instance._original_values = self.serialize_object(instance)

    def on_post_save_create(self, instance, sender, created, **kwargs):
        """Saves object's data."""
        if getattr(settings, 'DISABLE_AUDIT_TRAIL', False):
            return

        if not created:
            return

        if self.track_only_with_related and not self.is_parent_object_exists(instance):
            return

        audit_trail = AuditTrail.objects.generate_trail_for_instance_created(instance)
        audit_trail.changes = self.get_changes({}, self.serialize_object(instance))
        audit_trail.save()
        instance._original_values = self.serialize_object(instance)

        self.create_related_audit_trail(audit_trail)

    def on_post_save_update(self, instance, sender, created, **kwargs):
        """ Checks for difference and saves, if it's present. """
        if getattr(settings, 'DISABLE_AUDIT_TRAIL', False):
            return

        if created:
            return

        if self.track_only_with_related and not self.is_parent_object_exists(instance):
            return

        changes = self.get_changes(instance._original_values, self.serialize_object(instance))
        if not changes:
            return

        audit_trail = AuditTrail.objects.generate_trail_for_instance_updated(instance)
        audit_trail.changes = changes
        audit_trail.save()
        instance._original_values = self.serialize_object(instance)

        self.create_related_audit_trail(audit_trail)

    def on_pre_delete(self, instance, sender, **kwargs):
        """ Check if there related query_set that track current objects saves ids. """
        if getattr(settings, 'DISABLE_AUDIT_TRAIL', False):
            return

        if not self.notify_related:
            return
        instance._audit_ids_to_notify_related_deletion = {}
        for field_name in self.notify_related:
            parent_object = getattr(instance, field_name, None)
            if parent_object is None or hasattr(parent_object, '_meta'):
                continue
            if parent_object.all().exists():
                ids = list(parent_object.all().values_list('id', flat=True))
                instance._audit_ids_to_notify_related_deletion[field_name] = ids

    def on_post_delete(self, instance, sender, **kwargs):
        """ Saves deleted object data. """
        if getattr(settings, 'DISABLE_AUDIT_TRAIL', False):
            return

        if self.track_only_with_related and not self.is_parent_object_exists(instance):
            return

        audit_trail = AuditTrail.objects.generate_trail_for_instance_deleted(instance)
        audit_trail.changes = self.get_changes(self.serialize_object(instance), {})
        audit_trail.save()

        self.create_deleted_related_audit_trail(audit_trail, instance)

    def is_parent_object_exists(self, instance):
        for field_name in self.notify_related:
            parent_object = getattr(instance, field_name, None)
            if parent_object is None:
                continue
            if hasattr(parent_object, '_meta'):
                return True
            else:
                if parent_object.all().exists():
                    return True

                if field_name in getattr(instance, '_audit_ids_to_notify_related_deletion', {}):
                    return True

        return False

    def create_related_audit_trail(self, audit_trail):
        if not self.notify_related:
            return

        for field_name in self.notify_related:
            changed_related_object = audit_trail.content_object
            attribute = getattr(changed_related_object, field_name)
            if attribute is None:
                continue

            if hasattr(attribute, '_meta'):
                # Related object
                notified_objects = [attribute]
            else:
                # RelatedManager doesn't have _meta attribute
                notified_objects = attribute.all()

            for notified_object in notified_objects:
                parent_audit_trail = AuditTrail.objects.generate_trail_for_related_change(notified_object)
                parent_audit_trail.related_trail = audit_trail
                parent_audit_trail.save()

    def create_deleted_related_audit_trail(self, audit_trail, instance):
        if not self.notify_related:
            return

        for field_name in self.notify_related:
            attribute = getattr(instance, field_name)
            if attribute is None:
                continue

            if hasattr(attribute, '_meta'):
                # Related object
                notified_objects = [attribute]
            else:
                # RelatedManager doesn't have _meta attribute
                ids = instance._audit_ids_to_notify_related_deletion.get(field_name)

                if not ids:
                    continue
                # now parent object is being filtered by instance id
                # f.e.
                # class Post(models.Model):
                #     class Post(models.Model):
                #     author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
                #     audit = AuditTrailWatcher(track_related=['comment_set', 'author'])
                # will be filtered as {'author__exact': instance}
                # but since posts's author was set to null after author deletion we need to get posts by ids
                # so we stored ids before author deletion on pre_delete

                attribute.core_filters = {'id__in': ids}
                notified_objects = list(attribute.all())

            for notified_object in notified_objects:
                parent_audit_trail = AuditTrail.objects.generate_trail_for_related_change(notified_object)
                parent_audit_trail.related_trail = audit_trail
                parent_audit_trail.save()


@receiver(audit_trail_app_ready)
def init_audit_instances(*args, **kwargs):
    tracked_models = AuditTrailWatcher.tracked_models.copy()
    for model_class in tracked_models:
        model_class.audit.init_signals()

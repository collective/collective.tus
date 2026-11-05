from zope.publisher.interfaces.browser import IDefaultBrowserLayer


class ICollectiveTusLayer(IDefaultBrowserLayer):
    """Browser layer for collective.tus.

    The TUS services and the file field deserializer are registered for this
    layer, so they take over from plone.restapi's own once the add-on's
    profile is installed.
    """

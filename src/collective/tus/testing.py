from plone.app.testing import applyProfile
from plone.app.testing import FunctionalTesting
from plone.app.testing import IntegrationTesting
from plone.app.testing import PloneSandboxLayer
from plone.restapi.testing import PLONE_RESTAPI_DX_FIXTURE
from plone.testing import zope


class CollectiveTusLayer(PloneSandboxLayer):
    defaultBases = (PLONE_RESTAPI_DX_FIXTURE,)

    def setUpZope(self, app, configurationContext):
        import collective.tus

        self.loadZCML(package=collective.tus)

    def setUpPloneSite(self, portal):
        applyProfile(portal, "collective.tus:default")


COLLECTIVE_TUS_FIXTURE = CollectiveTusLayer()
COLLECTIVE_TUS_INTEGRATION_TESTING = IntegrationTesting(
    bases=(COLLECTIVE_TUS_FIXTURE,), name="CollectiveTusLayer:Integration"
)
COLLECTIVE_TUS_FUNCTIONAL_TESTING = FunctionalTesting(
    bases=(COLLECTIVE_TUS_FIXTURE, zope.WSGI_SERVER_FIXTURE),
    name="CollectiveTusLayer:Functional",
)

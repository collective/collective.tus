from setuptools import find_namespace_packages
from setuptools import setup


version = "1.0.0a1.dev0"

setup(
    name="collective.tus",
    version=version,
    description=(
        "TUS resumable uploads for plone.restapi: uploads moved into the blob "
        "instead of copied, and upload termination"
    ),
    long_description=(open("README.md").read() + "\n\n" + open("CHANGES.md").read()),
    long_description_content_type="text/markdown",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Web Environment",
        "Framework :: Plone",
        "Framework :: Plone :: Addon",
        "Framework :: Plone :: 6.0",
        "Framework :: Plone :: 6.1",
        "Framework :: Plone :: 6.2",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    keywords="plone restapi tus upload resumable",
    author="Jon Pentland, PretaGov [instification]",
    url="https://github.com/collective/collective.tus",
    project_urls={
        "Source": "https://github.com/collective/collective.tus",
        "Issues": "https://github.com/collective/collective.tus/issues",
        "Changelog": "https://github.com/collective/collective.tus/blob/main/CHANGES.md",
    },
    license_expression="GPL-2.0-or-later",
    license_files=["LICENSE"],
    packages=find_namespace_packages(where="src", include=["collective.*"]),
    package_dir={"": "src"},
    include_package_data=True,
    zip_safe=False,
    python_requires=">=3.10",
    install_requires=[
        # The add-on subclasses plone.restapi's TUS services and upload class.
        # They are internals, so the supported range is the one CI tests.
        "plone.restapi>=9.15,<11",
        "plone.namedfile",
        "Products.CMFCore",
        "Products.CMFPlone",
        "Products.GenericSetup",
        "ZODB",
    ],
    extras_require={
        "test": [
            "plone.app.testing",
            "plone.app.contenttypes",
            "plone.restapi[test]",
            "zope.testrunner",
        ],
        "lint": [
            "flake8",
            "black",
            "isort",
            "zpretty",
        ],
    },
    entry_points="""
    [z3c.autoinclude.plugin]
    target = plone
    [plone.autoinclude.plugin]
    target = plone
    module = collective.tus
    """,
)

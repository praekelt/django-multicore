from setuptools import setup, find_packages


setup(
    name="django-multicore",
    description="An app that makes it easy to parallelize Django code.",
    long_description = open("README.rst", "r").read() + open("AUTHORS.rst", "r").read() + open("CHANGELOG.rst", "r").read(),
    version="1.10.2",
    author="Praekelt Consulting",
    author_email="dev@praekelt.com",
    license="BSD",
    url="http://github.com/praekelt/django-multicore",
    packages = find_packages(),
    dependency_links = [
    ],
    install_requires = [
        "django",
        "dill"
    ],
    tests_require = [
        "tox",
    ],
    classifiers=[
        "Programming Language :: Python",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Framework :: Django",
        "Intended Audience :: Developers",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
    ],
    zip_safe=False,
)

from setuptools import setup

setup(
    name='odc_ppt',

    use_scm_version={"root": "../..", "relative_to": __file__},
    setup_requires=['setuptools_scm'],

    author='Open Data Cube',
    author_email='',
    maintainer='Open Data Cube',
    maintainer_email='',

    description='Various Parallel Processing Tools',
    long_description='',

    license='Apache License 2.0',

    tests_require=['pytest'],
    install_requires=[],

    packages=['odc.ppt'],
    zip_safe=False,
)

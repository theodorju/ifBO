from setuptools import setup, find_packages

setup(
    name="ifBO",  # Replace with your desired package name
    version="0.0.1",
    packages=find_packages(include=["mfpbench", "neps", "pfns4hpo", "pfns_hpo"]),
)

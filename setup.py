#!/usr/bin/env python3

from setuptools import setup, find_namespace_packages

setup(name="wb-update-manager",
      version="1.0",
      description="Wirenboard software updates and release management tool",
      author="Nikita Maslov",
      author_email="nikita.maslov@wirenboard.ru",
      packages=find_namespace_packages()
     )

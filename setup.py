#!/usr/bin/env python3

from setuptools import setup


def get_version():
    with open("debian/changelog", "r", encoding="utf-8") as f:
        return f.readline().split()[1][1:-1]


setup(
    name="wb-update-manager",
    version=get_version(),
    description="Wirenboard software updates and release management tool",
    license="MIT",
    author="Nikita Maslov",
    author_email="nikita.maslov@wirenboard.ru",
    maintainer="Wiren Board Team",
    maintainer_email="info@wirenboard.com",
    url="https://github.com/wirenboard/wb-update-manager",
    packages=["wb.update_manager"],
)

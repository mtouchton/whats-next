from setuptools import setup

APP = ['whatsnext.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'CFBundleName': "What's Next",
        'CFBundleDisplayName': "What's Next",
        'CFBundleIdentifier': 'com.whatsnext.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'LSUIElement': True,
        'LSMinimumSystemVersion': '13.0',
    },
    'packages': ['requests', 'keyring', 'rumps', 'charset_normalizer', 'certifi', 'urllib3', 'idna'],
    'includes': [
        'AppKit', 'Foundation', 'Security',
        'jaraco.classes', 'jaraco.functools', 'jaraco.context',
        'importlib_metadata', 'zipp', 'more_itertools',
        'backports', 'backports.tarfile',
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)

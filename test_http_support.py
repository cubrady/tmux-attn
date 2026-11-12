"""Explicit domain-only HTTP fixtures; production always uses real security/snapshots.

Existing route/domain tests isolate their original behavior through these injected
collaborators. Security and background collection have separate end-to-end tests.
"""
import attention as a


class DomainTestSecurity:
    @staticmethod
    def authorize(*_args):
        return None

    @staticmethod
    def authenticated(_headers):
        return True


class InlineTestFeeds:
    def __init__(self, app, include_local=True):
        self.app = app
        self.include_local = include_local

    def agents(self):
        return {**self.app.local_agents_payload(), 'version': self.app.install_version()}

    def host(self, name):
        return self.app.fleet_host_slice(name, include_local=self.include_local)

    def fleet(self):
        return self.app.web_later_fleet(self.app.fleet_payload(include_local=self.include_local))

    def status(self):
        return {}

    def invalidate(self, _host=''):
        pass


def domain_web_handler(app=a, **kwargs):
    return app.make_web_handler(**kwargs, security=DomainTestSecurity(),
                              snapshots=InlineTestFeeds(app, not kwargs.get('fleet_only', False)))

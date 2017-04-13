from django.apps import AppConfig

from multicore import initialize


class MulticoreAppConfig(AppConfig):
    name = "multicore"
    verbose_name = "Multicore"

    def ready(self):
        print "CALL INITIALIZE"
        initialize()

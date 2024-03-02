import os
import nuke

# I cannot remember where I got this code - if it is yours, please let
# me know so I can credit you here

class CustomPrefs(object):
    """
    Handle creation of custom prefs for a Nuke python extension.

    tab_label: string
               user-visible label of your tab
    knob_name_prefix: string
                      prefix to prepend to all knob names in knobs_list,
                      also used to generate the internal name of the tab
    knobs_list: list of knobs to add
               they will be added in order, as-is except
               their names will be prepended with knob_name_prefix
    version: string of form "X.X.X"
             if you are versioning your extension, this can be used to update
             the knobs automatically when you version up.
    """

    def __init__(
                 self,
                 tab_label,
                 knob_name_prefix,
                 knobs_list,
                 version="0.0.0",
                 ):
        super(CustomPrefs, self).__init__()

        self.tab_label = tab_label
        self.knob_name_prefix = knob_name_prefix
        self.knobs_list = knobs_list
        self.version = version

        self.p = nuke.toNode('preferences')
        self.preferences_wrapper = """Preferences {
        inputs 0
        name Preferences
        %s
        }
        """

        # actually set up the prefs
        if self.update_needed():
            self.update_knobs()
        else:
            self.add_knobs()

    def get_our_knobs(self):
        our_knobs = [
            k
            for knob_name, k in self.p.knobs().items()
            if knob_name.startswith(self.knob_name_prefix)
        ]
        return our_knobs

    def get_our_knob_names(self):
        our_knob_names = [
            k.name()
            for k in self.get_our_knobs()
        ]
        return our_knob_names

    def version_less_than(self, existing_version, new_version):
        """
        Compare each piece of a semantic version to determine if the existing
        version is less than the new version.
        """
        # TODO: make safe for versions of different lengths
        existing_version_int_list = [
            int(value)
            for value in existing_version.split('.')
            ]
        new_version_int_list = [
            int(value)
            for value in existing_version.split('.')
            ]
        for i, value in enumerate(existing_version_int_list):
            if value < new_version_int_list[i]:
                return True
        return False

    def get_existing_version(self):
        """
        Return the current semantic version on the preferences node, or "0.0.0"
        if it's not there.
        """
        try:
            version_knob_name = "{}_version".format(self.knob_name_prefix)
            version = self.p[version_knob_name].value()
            return version
        except NameError:
            return "0.0.0"

    def update_needed(self):
        if self.version == "0.0.0":
            return False
        if self.version_less_than(self.get_existing_version(), self.version):
            return True
        return False

    def update_knobs(self):
        """
        Helper for versioning your python extension. Looks for
        a knob called <knob_name_prefix>_version, and if it's less than
        the current version, updates the knobs. Expects versions of the
        form major.minor.patch
        """

        previous_settings = {
            k.name(): k.value()
            for k in self.get_our_knobs()
        }
        self.delete_knobs()
        self.add_knobs()
        for k in self.get_our_knobs():
            try:
                k.setValue(previous_settings[k.name()])
            except (AttributeError, KeyError, NameError):
                pass

    def create_tab(self):
        tab_name = "{}_tab".format(self.knob_name_prefix)
        if tab_name not in self.get_our_knob_names():
            tab_label = self.tab_label
            t = nuke.Tab_Knob(tab_name, tab_label)
            self.p.addKnob(t)

    def add_knob(self, k):
        mangled_name = "{}_{}".format(
            self.knob_name_prefix,
            k.name()
            )
        if mangled_name not in self.get_our_knob_names():
            k.setName(mangled_name)
            self.p.addKnob(k)

    def add_knobs(self):
        self.create_tab()
        for k in self.knobs_list:
            self.add_knob(k)
        self.save_prefs()

    def delete_knobs(self):
        """
        Delete all knobs managed by this CustomPrefs.
        """
        our_knobs = self.get_our_knobs()
        for k in our_knobs:
            self.p.removeKnob(k)
        self.save_prefs()

    def save_prefs(self):
        """
        Save preferences. Emulates functionality of "OK" button on
        Nuke preferences panel.
        """

        # Nuke does not provide this functionality, so it must emulated
        # uses % not .format to avoid having to escape {}
        preferences_contents = (
                self.preferences_wrapper
                % self.p.writeKnobs(
                    nuke.WRITE_USER_KNOB_DEFS
                    | nuke.WRITE_NON_DEFAULT_ONLY
                    | nuke.TO_SCRIPT
                    | nuke.TO_VALUE
                )
            )

        preferences_file_name = "preferences{}.{}.nk".format(
            nuke.NUKE_VERSION_MAJOR,
            nuke.NUKE_VERSION_MINOR,
        )
        preferences_dir = os.path.expandvars("$HOME/.nuke/")
        preferences_path = os.path.join(
            preferences_dir,
            preferences_file_name,
            )

        with open(preferences_path, 'w') as f:
            f.write(preferences_contents)

    def get_pref(self, name):
        mangled_name = "{}_{}".format(self.knob_name_prefix, name)
        try:
            return self.p[mangled_name].value()
        except NameError:
            return None

    def get_pref_knob(self, name):
        mangled_name = "{}_{}".format(self.knob_name_prefix, name)
        try:
            return self.p[mangled_name]
        except NameError:
            return None

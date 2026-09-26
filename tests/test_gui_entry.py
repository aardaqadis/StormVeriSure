"""The Tkinter entry point opens promptly without starting Workshop work."""

import sys
import unittest
from unittest.mock import patch

from stormcopy.__main__ import main


class GuiEntryTests(unittest.TestCase):
    def test_no_arguments_opens_gui_with_existing_workshop_database(self):
        with patch.object(sys, "argv", ["stormcopy"]), \
                patch("stormcopy.gui.launch") as launch, \
                patch("stormcopy.__main__.connect") as connect, \
                patch("stormcopy.__main__.discover_catalog") as discover, \
                patch("stormcopy.__main__.download_workshop") as download:
            main()

        launch.assert_called_once_with(db_path="workshop.sqlite")
        connect.assert_not_called()
        discover.assert_not_called()
        download.assert_not_called()

    def test_explicit_gui_paths_are_passed_to_launcher(self):
        argv = ["stormcopy", "--db", "custom.sqlite", "gui", "--workshop-folder",
                r"D:\SteamLibrary\steamapps\workshop\content\573090"]
        with patch.object(sys, "argv", argv), \
                patch("stormcopy.gui.launch") as launch, \
                patch("stormcopy.__main__.connect") as connect, \
                patch("stormcopy.__main__.discover_catalog") as discover, \
                patch("stormcopy.__main__.download_workshop") as download:
            main()

        launch.assert_called_once_with(db_path="custom.sqlite",
                                       workshop_folder=argv[-1])
        connect.assert_not_called()
        discover.assert_not_called()
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()

# Hi10_Cover_and_Donation_Image_Downloader
Allows staff to search and grab images from external sites like alphacoders. Zerochan is planned once they come back online.

# Easy Way
Head over to the releases tab and download the respective version for your OS of choice.
 
# Manual Installation
Any modern stable version of Python will work with this project. **At the time of writing this guide, it's currently [![Python](https://img.shields.io/badge/dynamic/json?label=Python&logo=python&logoColor=white&color=3776AB&url=https://endoflife.date/api/python.json&query=$[0].latest)](https://www.python.org/downloads/)**

**Linux users are unaffected by system path issues since Python is automatically added to the system path by default.**

**IMPORTANT NOTICE FOR WINDOWS USERS!**
**MAKE SURE YOU CHECK THE BOX LABELED "ADD PYTHON TO SYSTEM PATH" IN THE PYTHON INSTALLER. WITHOUT THAT SETUP, YOU WON'T BE ABLE TO CALL PYTHON OR PIP GLOBALLY ON YOUR SYSTEM.**

Once Python is fully installed, run the following command in cmd, PowerShell, or Terminal to install the required pip libraries.

NOTE: You can also run pip install -r requirements.txt if you're running the commands in the same location as the folder containing python postar.

**pip install requests PyQt6 beautifulsoup4 pillow packaging**

If you happen to get a message about updating pip after installing the needed dependencies, please feel free to update that while the terminal window is still active. However, it's not required for postar to function properly.

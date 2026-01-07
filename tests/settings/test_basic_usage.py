from typing import Literal
from alpenstock.settings import Settings
from pydantic import Field
from enum import Enum
from io import StringIO, BytesIO
import sys
import re as regex

Strategy = Literal['fast', 'balanced', 'unsafe']

class AppSettings(Settings):
    host: str = Field(default="localhost", description="The hostname of the server.")
    port: int = Field(default=8080, description="The port number the server listens on.")
    
    allowed_strategies: list[Strategy] = Field(
        default_factory=lambda: ['fast', 'balanced'],
        description=(
            "List of allowed strategies for processing. "
            "Choose from 'fast', 'balanced', or 'unsafe'."
            "The default strategies are 'fast' and 'balanced'."
            "When 'unsafe' is included, it may lead to potential risks."
        )
    )


def test_app_settings_defaults():
    s = AppSettings()
    
    # Dump without comments
    buf = StringIO()
    s.to_yaml(buf)
    yaml_output = buf.getvalue()
    
    expected_yaml = """\
host: localhost
port: 8080
allowed_strategies:
  - fast
  - balanced
"""
    assert yaml_output == expected_yaml
    
    # Fill comments and dump
    buf = StringIO()
    s.to_yaml(buf, fill_default_comments=True)
    yaml_output_with_comments = buf.getvalue()
    
    # Soft check for comments presence
    assert "# The hostname of the server." in yaml_output_with_comments
    assert "# The port number the server listens on." in yaml_output_with_comments
    assert "List of allowed strategies for processing." in yaml_output_with_comments


def test_basic_comment_preserving():
    yaml = """\
# __should_be_preserved_app_settings
host: example.com  # __should_be_preserved_host
port: 9090  # __should_be_preserved_port
allowed_strategies:
    - fast  # __not_preserved as it's in a list of non-Settings
    - unsafe
"""
    s = AppSettings.from_yaml(StringIO(yaml))
    assert s.host == "example.com"
    assert s.port == 9090
    assert s.allowed_strategies == ['fast', 'unsafe']
    
    # Dump back to YAML with comments preserved
    buf = StringIO()
    s.to_yaml(buf)
    dumped_yaml = buf.getvalue()
    
    print(dumped_yaml)
    
    assert "# __should_be_preserved_app_settings" in dumped_yaml
    assert "# __should_be_preserved_host" in dumped_yaml
    assert "# __should_be_preserved_port" in dumped_yaml
    assert "# __not_preserved" not in dumped_yaml


def test_key_order_preserving():
    yaml = """\
allowed_strategies:
    - balanced
port: 8081
host: myserver.com
"""
    s = AppSettings.from_yaml(StringIO(yaml))
    
    buf = StringIO()
    s.to_yaml(buf)
    dumped_yaml = buf.getvalue()
    
    pattern = regex.compile(
        r"^allowed_strategies:.*^port:.*^host:.*",
        regex.DOTALL | regex.MULTILINE
    )
    assert pattern.match(dumped_yaml)


def test_user_comment_with_default_comment():
    yaml = """\
# __should_be_preserved_app_settings
host: example.com  # __should_be_preserved_host
port: 9090  # __should_be_preserved_port
allowed_strategies:
    - fast  # __not_preserved as it's in a list of non-Settings
    - unsafe
"""
    s = AppSettings.from_yaml(StringIO(yaml))
    buf = StringIO()
    s.to_yaml(buf, fill_default_comments=True)
    
    dumped_yaml = buf.getvalue()

    # Both user and default comments should be present
    assert "# __should_be_preserved_app_settings" in dumped_yaml
    assert "# The hostname of the server." in dumped_yaml
    assert "# __should_be_preserved_host" in dumped_yaml
    assert "# The port number the server listens on." in dumped_yaml
    assert "# __should_be_preserved_port" in dumped_yaml


def test_dump_and_load_file(tmp_path):
    s = AppSettings()
    
    str_path = str(tmp_path / "str_path.yaml")
    s.to_yaml(str_path)
    s_loaded = AppSettings.from_yaml(str_path)
    assert s == s_loaded
    
    path_like_path = tmp_path / "path_like_path.yaml"
    s.to_yaml(path_like_path)
    s_loaded = AppSettings.from_yaml(path_like_path)
    assert s == s_loaded


def test_dump_and_load_io():
    s = AppSettings()
    
    # Dump and load using StringIO
    buf = StringIO()
    s.to_yaml(buf)
    buf.seek(0)
    s_loaded = AppSettings.from_yaml(buf)
    assert s == s_loaded
    
    # Dump and load using BytesIO
    buf = BytesIO()
    s.to_yaml(buf)
    buf.seek(0)
    s_loaded = AppSettings.from_yaml(buf)
    assert s == s_loaded
    
    # Load from bytes
    buf = BytesIO()
    s.to_yaml(buf)
    byte_data = buf.getvalue()
    s_loaded = AppSettings.from_yaml(byte_data)
    assert s == s_loaded
    

def test_list_of_settings():
    class Srv(Settings):
        dest: str = Field(default="localhost", description="Destination host.")
        port: int = Field(default=80, description="Destination port.")
    
    class UnixSrv(Settings):
        path: str = Field(default="/var/run/socket", description="Unix socket path.")
    
    class App(Settings):
        name: str = Field(default="MyApp", description="The name of the application.")
        srv: list[Srv | UnixSrv] = Field(
            default_factory=lambda: [Srv(), UnixSrv()],
            description="List of server configurations."
        )
    
    input_yaml = """\
srv:
  - dest: server1.com  # __should_be_preserved_dest
    port: 8080
  - path: /tmp/app.sock  # __should_be_preserved_path
name: TestApp
"""
    app = App.from_yaml(StringIO(input_yaml))
    assert app.name == "TestApp"
    assert len(app.srv) == 2
    assert isinstance(app.srv[0], Srv)
    assert app.srv[0].dest == "server1.com"
    assert app.srv[0].port == 8080
    assert isinstance(app.srv[1], UnixSrv)
    assert app.srv[1].path == "/tmp/app.sock"
    
    buf = StringIO()
    app.to_yaml(buf)
    dumped_yaml = buf.getvalue()
    assert dumped_yaml == input_yaml
    
    # insert a srv at the beginning.
    # comments should be preserved for existing items.
    app.srv.insert(0, Srv(dest="newserver.com", port=9090))
    buf = StringIO()
    app.to_yaml(buf)
    dumped_yaml = buf.getvalue()
    assert "# __should_be_preserved_dest" in dumped_yaml
    assert "# __should_be_preserved_path" in dumped_yaml
    


if __name__ == "__main__":
    test_list_of_settings()
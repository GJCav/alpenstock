# Comment Preserved Settings Management

The [`alpenstock.settings`](/alpenstock/reference/alpenstock/settings/) module adds YAML capabilities to `Pydantic`, which is an excellent library for data validation and settings management with Python type annotations. This module distinguishes itself from related libraries by the following features:

- **Comment Preservation**: All comments in the YAML files are preserved when loading and saving settings. Users are free to add comments to their configuration files, enhancing readability and maintainability. And developers can programmatically modify settings while keeping user comments intact.
- **Default Commenting**: The module allows developers to define default comments for each setting directly in the `Pydantic` model using the `description` attribute of `Field`. On saving, developers can choose to include these comments in the output YAML file, providing context and guidance for users. Note that when user-defined comments exist, they are preserved instead of being overwritten by default comments.
- **Key Order Preservation**: The order of keys in the YAML files is preserved when loading and saving settings. This ensures that the configuration files remain organized and easy to navigate, respecting the user's / developer's original structure.
- **Environment Variable Replacement**: The module supports replacing environment variable placeholders in the YAML files with their actual values from the system environment.

The following sections will guide you through the usage of these features with a crafted example.

## Installation

```bash
pip install alpenstock
```

## Recommended Usage

### Define `Settings` models

Now, imagine you are developing an application that requires configuration settings. You may want to define a settings model that includes various configuration options, along with comments to explain each setting. You may do this as follows:

```python
from alpenstock.settings import Settings
from pydantic import Field

class AppSettings(Settings):
    db: str = Field(default="sqlite:///default.db", description="Database connection string")
    passwd: str = Field(default="changeme", description="Database password")
    email: str = Field(default="user@example.com", description="User email address")

    srv: list["UnixSrv | HttpSrv"] = Field(
        default_factory=lambda: [HttpSrv(), UnixSrv()],
        description=(
            "List of server configurations. Each server can be either a Unix socket server "
            "or an HTTP server. For Unix socket, the 'socket_path' field specifies the "
            "socket file path. For HTTP server, 'host' and 'port' fields specify the "
            "listening address."
        )
    )

class UnixSrv(Settings):
    socket_path: str = Field(default="/var/run/app.sock")

class HttpSrv(Settings):
    host: str = Field(default="localhost")
    port: int = Field(default=8080)
```

**Key points**:

- All settings-related classes should inherit from
  [`alpenstock.settings.Settings`](/alpenstock/reference/alpenstock/settings/#alpenstock.settings.Settings).
  This base class extends `Pydantic`'s `BaseModel` with YAML-specific
  functionality.
- You may attach comments to each field using the `description` attribute of
  `Field`. These comments will be used as default comments when saving the
  settings to a YAML file.
- Nested settings models are supported, and are encouraged for complex
  configurations.

### Generate a template with comments

When a user first runs your application, he/she may want a template configuration file to get started. Instead of 
manually creating a YAML file, you can programmatically generate one with comments as follows:

```python
import sys

app = AppSettings()
app.to_yaml(sys.stdout, fill_default_comments=True, comment_width=50)
```

Here is the output YAML file:

```yaml
# Database connection string
db: sqlite:///default.db

# Database password
passwd: changeme

# User email address
email: user@example.com

# List of server configurations. Each server can be
# either a Unix socket server or an HTTP server. For
# Unix socket, the 'socket_path' field specifies the
# socket file path. For HTTP server, 'host' and
# 'port' fields specify the listening address.
srv:
  - host: localhost
    port: 8080
  - socket_path: /var/run/app.sock
```

**Key points:**

- The
  [`to_yaml`](/alpenstock/reference/alpenstock/settings/#alpenstock.settings.Settings.to_yaml)
  method is used to serialize the settings model to a YAML. Depending on the
  first parameter, the output can be directed to a file (indicated by a string
  or a path-like object), or to a `IO[bytes]`-like or `IO[str]`-like object.
- The comments are outputed above each setting, wrapped to the specified width,
  providing essential help for users.
- Check the [method
  documentation](/alpenstock/reference/alpenstock/settings/#alpenstock.settings.Settings.to_yaml)
  for more options.


### Replace environment variables

Users may want to avoid hardcoding sensitive information, such as passwords, in the YAML file. Instead, they can use environment variable placeholders, which will be replaced with actual values when loading the settings. For example, the user edits the `passwd` field in the generated YAML file as follows:

```yaml
# For security, refrain from hardcoding the password here.
# Load it from environment variable instead.
passwd: ${DB_PASSWD} 
```

Developers can enable environment variable replacement easily:

```python
import os
# simulate setting an environment variable
os.environ["DB_PASSWD"] = "s3cr3t_p@ssw0rd"

settings = AppSettings.from_yaml("path_to_settings.yaml", replace_env_vars=True)
print(settings.passwd)  # Output: s3cr3t_p@ssw0rd
```

**Key points:**

- The
  [`from_yaml`](/alpenstock/reference/alpenstock/settings/#alpenstock.settings.Settings.from_yaml)
  method is used to load settings from a YAML file.
- By setting the `replace_env_vars` parameter to `True`, any environment
  variable placeholders in the YAML file (formatted as `${VAR_NAME}`) will be
  replaced with their corresponding values from the environment.
- If an environment variable is not set, the placeholder becomes an empty string.

### Dump settings with comments and key order preserved

Assume the user has modified the generated YAML file to suit his/her needs,
adding comments along the way. For example:

```yaml
email: user@example.com  # Email is important so is moved to the top

db: postgresql://user@localhost/dbname  # Use PostgreSQL for better performance

passwd: example_pass

srv:
  - host: 127.0.0.1  # Bind to localhost
    port: 5555       # Use port 5555
```

Then, the user runs the application, and modifies some settings programmatically:

```python
settings = AppSettings.from_yaml("path_to_settings.yaml", replace_env_vars=True)

# To simulate some changes
settings.srv.insert(0, UnixSrv(socket_path="/tmp/app.sock"))

# Save the modified settings back to the YAML file 
# (simulated by output to stdout)
settings.to_yaml(sys.stdout)
```

The output YAML file will be:

```yaml
email: user@example.com  # Email is important so is moved to the top

db: postgresql://user@localhost/dbname  # Use PostgreSQL for better performance

passwd: example_pass

srv:
  - socket_path: /tmp/app.sock
  - host: 127.0.0.1  # Bind to localhost
    port: 5555       # Use port 5555
```

You can see that:

- All user-defined comments are preserved in their original locations.
- The order of keys is maintained as per the user's arrangement.

## Known Limitations

**Environment variable placeholders are missing in output**

Dumping after environment variable replacement will not preserve the placeholders in the output YAML file. Instead, the actual values from the environment will be written. 

Sometimes, this may not be the desired behavior, especially when sharing configuration files. However, in other scenarios, it may be acceptable or even preferred. For example, for the user to verify the actual values being used, a `settings show` command may be provided to output the effective configuration with all environment variables resolved.

So far, this limitation is known and accepted. Future versions may provide options to control this behavior.

**Comments in lists of non-Settings are dropped**

Currently, only when all items in a list are the subclasses of `Settings`, comments and orders are preserved. If the list contains non-Settings items (e.g., primitive types like `str`, `int`, etc.), comments associated with those items will be lost during load-and-dump round-trips.

The challenge here is that we cannot easily map items from the original list to
the new list. If it mismatches, it would be a disaster to merge the inner values and
comments. But if all items are `Settings` subclasses, there no need for such mapping
because each item can handle its own comments and order preservation internally.

So, developers are advised to use lists of `Settings` subclasses whenever
possible. Even for simple types, you may wrap them in a `Settings` subclass. For
example, instead of using `List[str]`, you may define a `StringItem(Settings)`
class with a single `value: str` field, and then use `List[StringItem]`.


## Implementation Details

The `alpenstock.settings` module leverages the [`ruamel.yaml`](https://pypi.org/project/ruamel.yaml/) library for YAML
parsing and serialization. This library supports comment preservation and key
order maintenance, which are essential for the features provided by this module.

When loading a YAML file, the module reads the content into a `CommentedMap`,
which retains comments and key order. The settings model is then populated from
this map, and stores the original `CommentedMap` as a private `_yaml` attribute
for later use.

When saving settings back to a YAML file, the module updates the stored `_yaml`
attribute with the current values from the settings models recursively, and adds
comments as needed. Finally, it serializes the updated `_yaml` back to
YAML format.

As the `_yaml` is preserved throughout the lifecycle of the settings model, all
user-defined comments and key orders are maintained seamlessly.

## Similar Libraries

- [pydantic-yaml](https://pypi.org/project/pydantic-yaml/): A library that
  integrates Pydantic with YAML, it supports exporting default comments to yaml
  but struggles to preserve user comments and key order in the load-and-dump
  round-trip. 


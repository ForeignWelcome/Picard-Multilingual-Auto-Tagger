# Installing with Arcane

This guide explains how to install Picard Multilingual Auto-Tagger using Arcane.

It assumes that:

- Arcane is already installed and connected to your Docker host
- `jlesage/musicbrainz-picard` is already running in Arcane or you will deploy it using "compose.picard-example.yaml" example
- You know the local IP address of your server
- You have created storage locations for the database and Ollama models

> The names and layout in Arcane may change slightly between versions.

## Before you begin

You need two writable host directories:

```text
/mnt/path/to/auto-tagger-data # for example   - /mnt/your-pool/configs/arabic-sort-data
/mnt/path/to/ollama-data # for example  - /mnt/your-pool/configs/ollama
```

The first directory stores the SQLite database containing verified corrections.

The second directory stores the downloaded Ollama models.

On TrueNAS, replace these examples with real dataset paths, such as:

```text
/mnt/pool/apps/auto-tagger-data or - /mnt/your-pool/configs/arabic-sort-data
/mnt/pool/apps/ollama or - /mnt/your-pool/configs/ollama
```

Do not use the example paths without changing them.

You also need to know your server IP address.

For example:

```text
192.168.1.100
```

---

## Method 1: Create the project manually

### 1. Open Projects

In Arcane, open:

```text
Projects
```

Click:

```text
Create Project
```

### 2. Choose a project name

Use:

```text
picard-multilingual-auto-tagger
```

### 3. Paste the Compose file

Copy the contents of the repository's main file:

```text
compose.yaml
```

Paste it into the Compose editor in Arcane.

### 4. Edit the required values

Change the database volume:

```yaml
volumes:
  - /mnt/path/to/auto-tagger-data:/data:rw
```

Change the Ollama storage volume:

```yaml
volumes:
  - /mnt/path/to/ollama-data:/root/.ollama:rw
```

Change the public URL:

```yaml
PUBLIC_BASE_URL: http://192.168.1.100:8787
```

Replace `192.168.1.100` with your server's actual IP address.

Check the user and group IDs:

```yaml
user: "568:568"
```

On TrueNAS, `568:568` is commonly used for applications. On another system, replace it with a user and group that can write to both storage directories.

Also change the timezone when needed:

```yaml
TZ: Asia/Tokyo
```

### 5. Add the source files

This project builds its own Docker image, so the following files must exist in the same Arcane project directory: for arcane you usually can copy those to your arcane stacks directory 

```text
auto-tagger/
├── Dockerfile
├── requirements.txt
└── app/
    ├── main.py
    └── picard_prefill.py
```

In Arcane, enable the project workspace

Create these folders:

```text
auto-tagger
auto-tagger/app
```

Upload the files into the matching locations: (you can use file browser for that)

```text
auto-tagger/Dockerfile
auto-tagger/requirements.txt
auto-tagger/app/main.py
auto-tagger/app/picard_prefill.py
```

The final Arcane project should look like this:

```text
picard-multilingual-auto-tagger/
├── compose.yaml
└── auto-tagger/
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py
        └── picard_prefill.py
```

### 6. Build and deploy

Click:

```text
save & create project
```

Arcane should build the local auto-tagger image and start:

```text
arabic-sort
arabic-sort-ollama
```

The internal names still use `arabic-sort` because the project was originally built for Arabic.

### 7. Download the Ollama model

Open the terminal or console for:

```text
arabic-sort-ollama
```

Run:

```bash
ollama pull qwen3:8b
```

Wait for the model download to complete.

You can confirm that it is installed with:

```bash
ollama list
```

### 8. Test the web interface

Open this address in a browser:

```text
http://YOUR-SERVER-IP:8787
```

For example:

```text
http://192.168.1.100:8787
```

The auto-tagger web interface should appear.

---

## Method 2: Create the project from GitHub

Newer Arcane versions can create a project directly from a Git repository.

### 1. Add the GitHub repository

In Arcane, open:

```text
Customization → Git Repositories
```

Click:

```text
Add Repository
```

Enter:

```text
https://github.com/ForeignWelcome/Picard-Multilingual-Auto-Tagger.git
```

Because the repository is public, authentication may not be required.

Save the repository.

### 2. Create a Git-synced project

Open:

```text
Projects
```

Use the dropdown beside **Create Project** and choose:

```text
From Git Repo
```

Choose:

```text
Repository:
Picard-Multilingual-Auto-Tagger

Branch:
main

Compose file:
compose.yaml
```

Use this project name:

```text
picard-multilingual-auto-tagger
```

Create the sync.

### 3. Edit the configuration

Before deploying, replace the example paths and IP address in the Compose configuration.

At minimum, configure:

```yaml
PUBLIC_BASE_URL: http://YOUR-SERVER-IP:8787
```

```yaml
- /mnt/path/to/auto-tagger-data:/data:rw
```

```yaml
- /mnt/path/to/ollama-data:/root/.ollama:rw
```

> Git-synced project files may be read-only inside Arcane. In that case, edit the repository files on GitHub, use an Arcane `.env` file, or create the project manually instead.

### 4. Build and deploy

Choose:

```text
Build & Deploy
```

After both containers start, open the `arabic-sort-ollama` console and run:

```bash
ollama pull qwen3:8b
```

---

# Connect the Picard container (create another project and use compose.picard-example.yaml)

The auto-tagger and Picard must share the same Docker network.

Open your existing Picard project in Arcane and edit its Compose file.

Use `compose.picard-example.yaml` from this repository as a reference.

## 1. Add the environment variables

Inside the Picard service's `environment` section, add:

```yaml
ARABIC_SORT_API_URL: http://arabic-sort:8787
ARABIC_SORT_PUBLIC_URL: http://YOUR-SERVER-IP:8787
```

Replace `YOUR-SERVER-IP` with your actual server IP.

For example:

```yaml
ARABIC_SORT_PUBLIC_URL: http://192.168.1.100:8787
```

## 2. Add Arabic font support

For Arabic titles, add:

```yaml
INSTALL_PACKAGES: font-noto-arabic
```

This is not required for every language, but the container must have a font capable of displaying the selected writing system.

## 3. Attach Picard to the shared network

Inside the Picard service, add:

```yaml
networks:
  - arabic-sort-network
```

At the bottom of the Picard Compose file, add:

```yaml
networks:
  arabic-sort-network:
    external: true
    name: arabic-sort-network
```

Save and redeploy the Picard project.

---

# Install the Picard plugin

Download:

```text
picard-plugin/arabic_tagging.zip
```

Copy it into the `import` folder inside your mounted Picard configuration directory.

For example, when the Picard configuration is mounted from:

```text
/mnt/pool/apps/picard
```

copy the ZIP to:

```text
/mnt/pool/apps/picard/import/arabic_tagging.zip
```

Then open Picard and go to:

```text
Options → Plugins
```

Choose:

```text
Install Plugin
```

Select `arabic_tagging.zip`, enable it, and restart Picard.

---

# Test the integration

1. Load or cluster an album in Picard.
2. Right-click the album or a track.
3. Select the multilingual auto-tagger action.
4. Confirm that the companion window opens.
5. Confirm that the artist, album, and track fields are populated.
6. Click **Generate**.
7. Review the generated title before copying it into Picard.

---

# Troubleshooting

## The project fails during the build

Confirm that Arcane has these files in the project:

```text
auto-tagger/Dockerfile
auto-tagger/requirements.txt
auto-tagger/app/main.py
auto-tagger/app/picard_prefill.py
```

Also confirm that `compose.yaml` contains:

```yaml
build:
  context: ./auto-tagger
  dockerfile: Dockerfile
```

## Picard cannot connect to the auto-tagger

Confirm that both projects use:

```text
arabic-sort-network
```

The Picard container should use the internal address:

```text
http://arabic-sort:8787
```

Do not replace this internal address with your server IP.

## The plugin opens the wrong address

Check:

```yaml
ARABIC_SORT_PUBLIC_URL: http://YOUR-SERVER-IP:8787
```

This value must be reachable from the computer running your browser.

## No titles are generated

Confirm that the model is installed:

```bash
ollama list
```

You should see:

```text
qwen3:8b
```

Also check the logs for:

```text
arabic-sort
arabic-sort-ollama
```

## Permission denied for the database

Confirm that the configured user has permission to write to:

```text
/mnt/path/to/auto-tagger-data
```

On TrueNAS, also check the dataset ownership and permissions.

## Arabic characters appear as boxes

Confirm that the Picard container includes:

```yaml
INSTALL_PACKAGES: font-noto-arabic
```

Then recreate or redeploy the Picard container.

---

# Updating later

When using a manually created Arcane project:

1. Download the updated repository files.
2. Replace the relevant project files.
3. Click **Build & Deploy** again.

When using Git sync:

1. Sync or pull the latest repository version.
2. Review changes to the Compose file.
3. Redeploy the project.

Back up the SQLite database before major updates:

```text
/data/arabic_sort.db
```

The database contains your verified title corrections.

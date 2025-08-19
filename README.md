Development environment for [Protwis](https://github.com/protwis/protwis) using Vagrant and Puppet.

### Instructions

This guide describes how to set up a ready-to-go virtual machine with Virtualbox and Vagrant.

Works on Linux, Mac, and Windows.

#### Prerequisites

* [Vagrant][vagrant]
* [Virtualbox][virtualbox]
* [Git][git]
* [GitHub][github] account

[vagrant]: http://www.vagrantup.com
[virtualbox]: https://www.virtualbox.org
[git]: http://git-scm.com
[github]: http://www.bitbucket.org

Install Vagrant, VirtualBox, and Git, and create a GitHub account (if you don't already have one).

Make sure you have the latest version of all three. On Ubuntu (and this may also apply to other Linux distros), the
package manager installs an old version of Vagrant, so you will have to download and install the latest version from
the Vagrant website.

#### Linux and Mac

##### Clone the protwis_vagrant repository from GitHub

Open up a terminal and type

    git clone --recursive https://github.com/protwis/protwis_vagrant.git ~/protwis_vagrant
    cd ~/protwis_vagrant

##### Fork the protwis repository

Go to https://github.com/protwis/protwis and click "Fork" in the top right corner

##### Clone the forked repository

Clone into the "shared" directory (replace your-username with your GitHub username)

    cd ~/protwis_vagrant
    git clone https://github.com/your-username/protwis.git shared/sites/protwis

##### Add vagrant plugin

This allows for VM boxes to change disk size

    vagrant plugin install vagrant-disksize

##### Start the vagrant box

This may take a few minutes

    vagrant up

##### Log into the vagrant VM

    vagrant ssh

##### Start the built in Django development webserver

    cd /protwis/sites/protwis
    /env/bin/python3 manage.py runserver 0.0.0.0:8000

You're all set up. The webserver will now be accessible from http://localhost:8000

#### Windows

##### Clone the protwis_vagrant repository from GitHub

Open up a shell and type

    git clone --recursive https://github.com/protwis/protwis_vagrant.git .\protwis_vagrant
    cd .\protwis_vagrant

##### Fork the protwis repository

Go to https://github.com/protwis/protwis and click "Fork" in the top right corner

##### Clone the forked repository

Clone into the "shared" directory (replace your-username with your GitHub username)

    cd ~/protwis_vagrant
    git clone https://github.com/your-username/protwis.git .\shared\sites\protwis

##### Start the vagrant box

This may take a few minutes

    vagrant up

##### Log into the vagrant VM

Use an SSH client, e.g. PuTTY, with the following settings

    host: 127.0.0.1
    port: 2226
    username: vagrant
    password: vagrant

##### Start the Django development webserver

    cd /protwis/sites/protwis
    /env/bin/python3 manage.py runserver 0.0.0.0:8000

You're all set up. The webserver will now be accessible from http://localhost:8000

#### Other notes

The protwis directory is now shared between the host machine and the virtual machine, and any changes made on the host
machine will be instantly reflected on the server.

To run django commands from the protwis directory, ssh into the VM, and use the "/env/bin/python3" command e.g

    cd ~/protwis_vagrant/
    vagrant ssh
    cd /protwis/sites/protwis
    /env/bin/python3 manage.py check protein

The database administration tool Adminer is installed and accessible at http://localhost:8001/adminer. Use the
following settings

    System: PostgreSQL
    Server: localhost
    Username: protwis
    Password: protwis

### RDKit cartridge update of GPCRdb development environment and GPCRdb Vagrant and servers

From Release [November 2024](https://github.com/protwis/protwis/releases/tag/v2024_11), **RDKit cartridge** for **PostgreSQL** and **django-rdkit** python library are required.

For installing django-rdkit after loging in into the vagrant VM through a SSH terminal, run this command:

    /env/bin/pip install git+https://github.com/rdkit/django-rdkit.git

Two installation methods for RDKit cartridge for PostgreSQL are describe next: the [Docker method](https://github.com/ismaelresp/protwis_vagrant/edit/rdkit_cartridge_install_instrucctions_dirty/README.md#docker-method) and the [Manual instalation method](https://github.com/ismaelresp/protwis_vagrant/edit/rdkit_cartridge_install_instrucctions_dirty/README.md#manual-instalation-method).

#### Docker method
TO BE DONE

#### Manual instalation method

**First**, with the Vagrant VM halted, edit the line `vb.customize ["modifyvm", :id, "--memory", "4096"]` file ~/protwis_vagrant/Vagrantfile, replacing "4096" by 2048 multiplied by the number of CPUs asigned in Vagrantfile ("8096" for 4 CPUs). The number can be set back to its original value after **RDKit cartridge** for **PostgreSQL** installation.

**Second**, after a `vagrant up` and logging in into the vagrant VM through a SSH terminal (as "vagrant" user), follow these steps:

1. Download the [install.tar.gz](https://github.com/ismaelresp/docker-postgres-rdkit/releases/download/v0.0.4/install.tar.gz) file containing the installation scripts into a folder in the vagrant user home directory running the following commands in the SSH terminal:
    ```
    mkdir ~/rdkit_cartridge
    /usr/bin/wget -q https://github.com/ismaelresp/docker-postgres-rdkit/releases/download/v0.0.4/install.tar.gz -O ~/rdkit_cartridge/install.tar.gz
    ```
2. Extract the files from install.tar.gz running the following commands in the SSH terminal:
    ```
    cd ~/rdkit_cartridge/
    tar xvzf install.tar.gz
    ```
3. Make a backup of ~/.bashrc running the following command in the SSH terminal:
    ```
    cp ~/.bashrc ~/.bashrc.rdkit_cartridge_user_bkp
    ```
4. Run the script to install *conda* running the following command in the SSH terminal (**if you have previously installed *conda* on your own** inside the Vagrant Virtual Machine guest OS, read first https://github.com/ismaelresp/docker-postgres-rdkit/blob/master/install/README.md):
    ```
    cd ~/rdkit_cartridge/install
    sudo bash install_conda_rdkit_cartridge.sh
    ```
    **IMPORTANT:** If *install_conda_rdkit_cartridge.sh* fails, restore the *~/.bashrc* file as explained at the end of the page https://github.com/ismaelresp/docker-postgres-rdkit/blob/master/install/README.md (running all commands in the SSH terminal) before repeating this step or running again *install_conda_rdkit_cartridge.sh*.

5. Run 'bash' in the SSH terminal.

**CAUTION:** Next step **ERASES** the folder, subfolders and its contents or the file in the path *$RDBASE*, a variable set up in `config.sh`, */rdkit* by default.

**CAUTION:** Next step **ERASES** the folder, subfolders and its contents or the file in the path *rdkit-*${*RDKIT_VERSION*}*.tar.gz* in the current working directory. ${*RDKIT_VERSION*} is a variable set up in `config.sh`, and its value follows by default the pattern  *Release_yyyy_mm_n* , where *y*, *m* and *n* are numbers.

**CAUTION:** Next step **ERASES** the file ${*RDKIT_VERSION*}*.tar.gz* in the current working directory. ${*RDKIT_VERSION*} is a variable set up in `config.sh`, and its value follows by default the pattern  *Release_yyyy_mm_n* , where *y*, *m* and *n* are numbers.

6. Run in the SSH terminal `sudo bash -i install_rdkit_cartridge.sh`.

7. Close the SSH terminal by running in the terminal the command 'exit' two times, and run 'vagrant reload' in the guest terminal in order to reboot the Vagrant Virtual Machine.

8. After loging in into the vagrant VM through a SSH terminal (as "vagrant" user), run in the SSH terminal:
    ```
    cd ~/rdkit_cartridge/install
    sudo bash create_rdkit_extension.sh
    ```
9. RECOMMENDED: Follow the next steps for testing your installation.
10. Install install vim and/or nano from command line running in the SSH terminal:
    ```
    sudo apt update
    sudo apt install vim
    sudo apt install nano
    ```
12. Edit *pg_hba.conf* (usually found at /etc/postgresql/[MAJOR_POSTGRES_VERSION]/main/pg_hba.conf) using `sudo vim /path/to/pg_hba.conf` or `sudo nano /path/to/pg_hba.conf` commands in the SSH terminal (if needed, look for a tutorial on how to edit a file with vim or nano) so the following line is present and uncommented:

        local	all      all          peer

    Comment (by adding # at the beggining of the line) or delete any other line that begins with "local	all      all".

    Alternatively, the following line in *pg_hba.conf* should be present and uncommented replacing POSTGRES_USER by the value of the *POSTGRES_USER* variable in `~/rdkit_cartridge/install/config.sh`. Default *rdkit-test*:

        local	all      POSTGRES_USER          peer

    Comment or delete any other line that begins with "local	all      POSTGRES_USER", where POSTGRES_USER is the value of the *POSTGRES_USER* variable in `~/rdkit_cartridge/install/config.sh`.
    
    These pg_hba.conf settings are only needed for installing and testing installation.

13. Close the SSH terminal by running in the terminal the command 'exit', and run 'vagrant reload' in the guest terminal in order to reboot the Vagrant Virtual Machine.
14. After loging in into the vagrant VM through a SSH terminal (as "vagrant" user), change the working directory of your SSH terminal by running the command `cd ~/rdkit_cartridge/install`.

15. Follow [Testing installation instructions](https://github.com/ismaelresp/docker-postgres-rdkit/blob/master/install/README.md#testing-installation-recommended) and the [Restoring ~/.bashrc instructions](https://github.com/ismaelresp/docker-postgres-rdkit/blob/master/install/README.md#restoring-bashrc) at https://github.com/ismaelresp/docker-postgres-rdkit/blob/master/install/README.md .

    The `config.sh` file mentioned in such instructions is `~/rdkit_cartridge/install/config.sh`. The contents of the mentioned file can that can be displayed with the command `cat ~/rdkit_cartridge/install/config.sh`.

For manual installation troubleshooting, please read https://github.com/ismaelresp/docker-postgres-rdkit/blob/master/install/README.md .

#### Performance optimization
For performace optimization check https://github.com/ismaelresp/docker-postgres-rdkit?tab=readme-ov-file#performance-optimization .

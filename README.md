Development environment for [Protwis](https://github.com/protwis/protwis) using Vagrant and Puppet.

### Instructions

This guide describes how to set up a ready-to-go virtual machine (VM) with Virtualbox and Vagrant.

Works on Linux, Mac, and Windows.

#### Prerequisites

* [Vagrant][vagrant]
* [Virtualbox][virtualbox]
* [Git][git]
* [GitHub][github] account

[vagrant]: https://www.vagrantup.com
[virtualbox]: https://www.virtualbox.org
[git]: https://git-scm.com
[github]: https://github.com

Install **Vagrant**, **VirtualBox**, and **Git**, and create a **GitHub** account (if you don't already have one).

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

##### Add vagrant plugins

This allows for VM boxes to change disk size

    vagrant plugin install vagrant-disksize

To mount your folders into the vagrant VM you will also need to install the guest additions plugin 

    vagrant plugin install vagrant-vbguest    

##### Start the vagrant box

This may take a few minutes

    vagrant up

##### Log into the vagrant VM

    vagrant ssh

##### Activate the python virtual environment and start the built in Django development webserver

    cd /protwis/sites/protwis
    source /env/bin/activate
    ./manage.py runserver 0.0.0.0:8000

You're all set up. The web server will now be accessible in your local web browser at http://localhost:8000

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

You're all set up. The web server will now be accessible in your local web browser at http://localhost:8000

#### Other notes

The protwis directory is now shared between the *host* machine and the *virtual* machine, and any changes made on the
host machine will be instantly reflected on the server.

To run django commands from the protwis directory, ssh into the VM, and activate the python virtual environment, 
`source /env/bin/activate` then you can start running django e.g

    cd ~/protwis_vagrant/
    vagrant ssh
    cd /protwis/sites/protwis
    source /env/bin/activate
    ./manage.py check protein

The database administration tool **Adminer** is installed and accessible at http://localhost:8001/adminer. Use the
following settings

    System: PostgreSQL
    Server: localhost
    Username: protwis
    Password: protwis

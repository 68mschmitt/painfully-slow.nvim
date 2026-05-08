" Apple IIe remote terminal - vim config
set nocompatible
set term=vt100

set number rnu

set expandtab
set tabstop=4
set shiftwidth=4
set softtabstop=4

set smartindent
set autoindent

set showmatch

set backspace=indent,eol,start

syntax on

set ignorecase
set smartcase

set incsearch
set hlsearch

set encoding=utf-8

" Persist undo (resolves to project home/.vim/undodir)
set undofile
set undodir=~/.vim/undodir

" Show trailing whitespace
set list
set listchars=tab:>\ ,trail:-

set ruler
set laststatus=2

set scrolloff=5
set sidescrolloff=5

set showcmd
set showmode
